#!/usr/bin/env python3
"""
Test: Persistent AllReduce (no process respawn).

Validates that Gloo's collective_id auto-increment works by running
multiple AllReduce iterations within a SINGLE init/destroy lifecycle.

This is the minimal test for the pair.cc collective_id++ change.
Compare results with the existing spawn-per-iteration test.

Usage:
    bash run_persistent_test.sh [starting_cid]
"""

import os
import time
import torch
import torch.distributed as dist
import multiprocessing
import numpy as np

MASTER_ADDR = "127.0.0.1"
MASTER_PORT = "29500"
WORLD_SIZE = 8
ITERATIONS = int(os.environ.get("UDP_MOD_ITERATIONS", "8"))
NUM_CHUNKS = int(os.environ.get("NUM_CHUNKS", "4"))
ELEMENTS_PER_CHUNK = 256  # FP32: 256 elements per 1KB chunk
TENSOR_SIZE = NUM_CHUNKS * ELEMENTS_PER_CHUNK

OP_TYPE_SUM = 0x05
OP_TYPE_AVERAGE = 0x06


def persistent_worker(rank, time_queue):
    """
    Single worker that runs MULTIPLE AllReduce calls within
    one init_process_group / destroy_process_group lifecycle.

    This tests the collective_id auto-increment in pair.cc.
    Follows the same env setup as test_allreduce_fp_formats.py run_node().
    """
    # --- Environment (identical to run_node in test_allreduce_fp_formats.py) ---
    os.environ["MASTER_ADDR"] = MASTER_ADDR
    os.environ["MASTER_PORT"] = MASTER_PORT
    os.environ["WORLD_SIZE"] = str(WORLD_SIZE)
    os.environ["RANK"] = str(rank)
    os.environ["GLOO_SOCKET_IFNAME"] = "lo"
    os.environ["USE_FBGEMM"] = "0"

    if "FPGA_HOST" not in os.environ:
        os.environ["FPGA_HOST"] = "127.0.0.1"

    os.environ["UDP_MOD_MAX_LEVEL"] = "3"
    os.environ["UDP_MOD_RESPONSE_LEVEL"] = "4"

    # Read operation type from env (matches fp_formats pattern)
    op_type = int(os.environ.get("UDP_MOD_OPERATION", "0x05"), 0)

    # Init Gloo ONCE — collective_id auto-increments from here
    dist.init_process_group("gloo", rank=rank, world_size=WORLD_SIZE)

    if rank == 0:
        base_cid = int(os.environ.get("UDP_MOD_COLLECTIVE_ID", "0"), 0)
        op_name = "AVERAGE" if op_type == OP_TYPE_AVERAGE else "SUM"
        print(f"  [Rank 0] Gloo initialized. Starting CID=0x{base_cid:04X}, Op={op_name}")
        print(f"  [Rank 0] Running {ITERATIONS} iterations WITHOUT respawning\n")

    for i in range(ITERATIONS):
        torch.manual_seed(1337 + rank + i)
        tensor = torch.rand(TENSOR_SIZE, dtype=torch.float32)

        dist.barrier()

        start = time.perf_counter()
        dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
        elapsed_ms = (time.perf_counter() - start) * 1000

        if rank == 0:
            # Verify: compute expected result (matches fp_formats verification)
            expected = torch.zeros(TENSOR_SIZE)
            for r in range(WORLD_SIZE):
                torch.manual_seed(1337 + r + i)
                expected += torch.rand(TENSOR_SIZE, dtype=torch.float32)

            # Account for AVERAGE operation (hardware divides by N at final level)
            if op_type == OP_TYPE_AVERAGE:
                expected = expected / WORLD_SIZE

            max_diff = (tensor - expected).abs().max().item()
            tolerance = 1e-4
            status = "OK" if max_diff < tolerance else "FAIL"

            print(f"  Iter {i}: Status={status} (MaxDiff={max_diff:.6f}) Time={elapsed_ms:.1f}ms")
            time_queue.put(elapsed_ms)

    dist.destroy_process_group()

    if rank == 0:
        print(f"\n  [Rank 0] All {ITERATIONS} iterations complete. Process group destroyed.")


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")

    op_type = int(os.environ.get("UDP_MOD_OPERATION", "0x05"), 0)
    op_name = "AVERAGE" if op_type == OP_TYPE_AVERAGE else "SUM"

    print(f"=== Persistent AllReduce Test ===")
    print(f"  Nodes: {WORLD_SIZE}, Iterations: {ITERATIONS}, Chunks: {NUM_CHUNKS}")
    print(f"  CID: {os.environ.get('UDP_MOD_COLLECTIVE_ID', '0x0000')}, Op: {op_name}")
    print(f"  Mode: PERSISTENT (no respawn)\n")

    time_queue = multiprocessing.Queue()

    processes = []
    for rank in range(WORLD_SIZE):
        p = multiprocessing.Process(target=persistent_worker, args=(rank, time_queue))
        p.start()
        processes.append(p)

    for p in processes:
        p.join(timeout=300)
        if p.is_alive():
            print("TIMEOUT — killing")
            p.terminate()

    times = []
    while not time_queue.empty():
        times.append(time_queue.get())

    print(f"\n{'='*50}")
    if times:
        print(f"Results: {len(times)}/{ITERATIONS} OK")
        print(f"  Avg: {np.mean(times):.1f}ms, Min: {np.min(times):.1f}ms, Max: {np.max(times):.1f}ms")
    else:
        print("No results collected!")
    print(f"{'='*50}")
