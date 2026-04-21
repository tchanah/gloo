#!/usr/bin/env python3
"""
Test: AVERAGE with negative values (gradient-like data).

The persistent AllReduce test passes with torch.rand [0,1] (positive only),
but training with AVERAGE produces NaN. This test isolates whether
NEGATIVE values cause the AVERAGE hardware to fail.

Tests 3 scenarios:
  1. Positive only: torch.rand [0, 1]       — known working
  2. Mixed sign:    torch.randn (mean=0)     — gradient-like
  3. Scaled mixed:  torch.randn * 0.01       — small gradient-like

Usage:
    bash run_persistent_test.sh  (set UDP_MOD_OPERATION=0x06 for AVERAGE)
    OR: python3 test_average_negative.py
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
NUM_CHUNKS = int(os.environ.get("NUM_CHUNKS", "1024"))
ELEMENTS_PER_CHUNK = 256
TENSOR_SIZE = NUM_CHUNKS * ELEMENTS_PER_CHUNK

OP_TYPE_SUM = 0x05
OP_TYPE_AVERAGE = 0x06

# Test scenarios
SCENARIOS = [
    ("positive_rand",  lambda rank, i: torch.rand(TENSOR_SIZE)),
    ("mixed_randn",    lambda rank, i: torch.randn(TENSOR_SIZE)),
    ("small_randn",    lambda rank, i: torch.randn(TENSOR_SIZE) * 0.01),
    ("negative_only",  lambda rank, i: -torch.rand(TENSOR_SIZE)),
    ("large_range",    lambda rank, i: torch.randn(TENSOR_SIZE) * 10.0),
]


def test_worker(rank, result_queue):
    """Worker that tests multiple value distributions with AVERAGE."""

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

    op_type = int(os.environ.get("UDP_MOD_OPERATION", "0x06"), 0)

    dist.init_process_group("gloo", rank=rank, world_size=WORLD_SIZE)

    if rank == 0:
        base_cid = int(os.environ.get("UDP_MOD_COLLECTIVE_ID", "0"), 0)
        op_name = "AVERAGE" if op_type == OP_TYPE_AVERAGE else "SUM"
        print(f"  [Rank 0] Initialized. CID=0x{base_cid:04X}, Op={op_name}")
        print(f"  [Rank 0] Testing {len(SCENARIOS)} scenarios, 2 iterations each\n")

    for scenario_name, gen_fn in SCENARIOS:
        for i in range(2):
            # Generate data with fixed seed per rank+iteration for reproducibility
            torch.manual_seed(42 + rank * 100 + i)
            tensor = gen_fn(rank, i)

            # Record pre-AR stats
            pre_min = tensor.min().item()
            pre_max = tensor.max().item()
            pre_has_nan = torch.isnan(tensor).any().item()

            dist.barrier()

            start = time.perf_counter()
            dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
            elapsed_ms = (time.perf_counter() - start) * 1000

            if rank == 0:
                # Compute expected result
                expected = torch.zeros(TENSOR_SIZE)
                for r in range(WORLD_SIZE):
                    torch.manual_seed(42 + r * 100 + i)
                    expected += gen_fn(r, i)

                if op_type == OP_TYPE_AVERAGE:
                    expected = expected / WORLD_SIZE

                has_nan = torch.isnan(tensor).any().item()
                has_inf = torch.isinf(tensor).any().item()
                max_diff = (tensor - expected).abs().max().item()
                status = "OK" if (max_diff < 1e-3 and not has_nan and not has_inf) else "FAIL"

                print(f"  {scenario_name} iter{i}: {status} "
                      f"MaxDiff={max_diff:.6f} NaN={has_nan} Inf={has_inf} "
                      f"PreRange=[{pre_min:.3f},{pre_max:.3f}] "
                      f"PostRange=[{tensor.min().item():.3f},{tensor.max().item():.3f}] "
                      f"Time={elapsed_ms:.0f}ms")

                result_queue.put({
                    "scenario": scenario_name,
                    "iter": i,
                    "status": status,
                    "max_diff": max_diff,
                    "nan": has_nan,
                    "inf": has_inf,
                    "time_ms": elapsed_ms,
                })

    dist.destroy_process_group()

    if rank == 0:
        print(f"\n  [Rank 0] All scenarios complete.")


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")

    op_type = int(os.environ.get("UDP_MOD_OPERATION", "0x06"), 0)
    op_name = "AVERAGE" if op_type == OP_TYPE_AVERAGE else "SUM"

    print(f"=== AVERAGE Negative Value Test ===")
    print(f"  Op: {op_name}, Chunks: {NUM_CHUNKS}")
    print(f"  CID: {os.environ.get('UDP_MOD_COLLECTIVE_ID', '?')}")
    print(f"  Scenarios: {', '.join(s[0] for s in SCENARIOS)}\n")

    result_queue = multiprocessing.Queue()

    processes = []
    for rank in range(WORLD_SIZE):
        p = multiprocessing.Process(target=test_worker, args=(rank, result_queue))
        p.start()
        processes.append(p)

    for p in processes:
        p.join(timeout=300)
        if p.is_alive():
            print(f"TIMEOUT — killing {p.pid}")
            p.terminate()

    # Collect results
    results = []
    while not result_queue.empty():
        results.append(result_queue.get())

    print(f"\n{'='*60}")
    print(f"Summary:")
    for r in results:
        print(f"  {r['scenario']:20s} iter{r['iter']}: {r['status']} "
              f"MaxDiff={r['max_diff']:.6f} Time={r['time_ms']:.0f}ms")

    failures = [r for r in results if r['status'] == 'FAIL']
    if failures:
        print(f"\n  ❌ {len(failures)} FAILURES — AVERAGE has issues with these value patterns")
    else:
        print(f"\n  ✅ All scenarios passed")
    print(f"{'='*60}")
