
import os
import torch
import torch.distributed as dist
import multiprocessing
import time
import socket

# Configuration
MASTER_ADDR = "127.0.0.1"
MASTER_PORT = "29500"
WORLD_SIZE = 8
ITERATIONS = 1024

def run_node(rank, size):
    # Environment Setup
    os.environ["MASTER_ADDR"] = MASTER_ADDR
    os.environ["MASTER_PORT"] = MASTER_PORT
    os.environ["WORLD_SIZE"] = str(size)
    os.environ["RANK"] = str(rank)
    
    # Interfaces - Match user's environment
    # Use loopback for strictly local simulation to avoid "Connection refused" on external IP
    os.environ["GLOO_SOCKET_IFNAME"] = "lo" 
    
    # User Preferences
    os.environ["USE_FBGEMM"] = "0"
    
    # UDP Mod Configuration for FireSim
    # Ensure these point to your FireSim host
    # UDP_MOD_PORT is usually 5684 (default)
    # FPGA_HOST must be set to the IP of the machine running the FireSim switch
    # We'll assume localhost for now, or inherit from shell if set
    if "FPGA_HOST" not in os.environ:
         os.environ["FPGA_HOST"] = "127.0.0.1"

    # Hardware Configuration
    os.environ["UDP_MOD_MAX_LEVEL"] = "3"
    os.environ["UDP_MOD_RESPONSE_LEVEL"] = "4"
         
    # Get base collective ID from environment, default to 1
    base_collective_id = int(os.environ.get("UDP_MOD_COLLECTIVE_ID", "1"), 0)

    # 1 Chunk = 256 elements (1024 bytes)
    num_chunks = int(os.environ.get("NUM_CHUNKS", "4"))
    tensor_size = num_chunks * 256
    
    for i in range(ITERATIONS):
        # Update Collective ID for this iteration (to trigger hardware reset/new op)
        current_id = base_collective_id + i
        os.environ["UDP_MOD_COLLECTIVE_ID"] = str(current_id)
        
        # Initialize Gloo (creates new Pair objects which read the new env var)
        # We must re-init per iteration because Pair constructor reads env var once
        dist.init_process_group("gloo", rank=rank, world_size=size)
        
        if rank == 0:
            print(f"[Node {rank}] Iter {i}: ID={hex(current_id)} started for {tensor_size} elements")

        # Setup Tensor based on NUM_CHUNKS 
        tensor = torch.full((tensor_size,), float(rank), dtype=torch.float32)

        # Barrier to sync before measurement
        dist.barrier()
        
        start_time = time.perf_counter()
        dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
        end_time = time.perf_counter()
        
        elapsed = (end_time - start_time) * 1000
        
        # Verify result (should be 28.0 for 8 nodes)
        expected_sum = sum(range(size))
        current_val = tensor[0].item()
        
        status = "OK" if abs(current_val - expected_sum) < 0.001 else "FAIL"
        print(f"[Node {rank}] Iter {i}: Val={current_val} ({status}) Time={elapsed:.2f}ms ID={hex(current_id)}")
    
        # Destroy process group to force cleanup and re-creation next time
        dist.destroy_process_group()

if __name__ == "__main__":
    processes = []
    multiprocessing.set_start_method("spawn")
    
    print(f"Starting {WORLD_SIZE}-node AllReduce Test for FireSim...")
    
    for rank in range(WORLD_SIZE):
        p = multiprocessing.Process(target=run_node, args=(rank, WORLD_SIZE))
        p.start()
        processes.append(p)
        
    for p in processes:
        p.join()
        
    print("Test Complete.")
