
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
ITERATIONS = 64

def run_node(rank, size, iteration_id, time_queue):
    """Run a single iteration of AllReduce for one node."""
    # Environment Setup
    os.environ["MASTER_ADDR"] = MASTER_ADDR
    os.environ["MASTER_PORT"] = MASTER_PORT
    os.environ["WORLD_SIZE"] = str(size)
    os.environ["RANK"] = str(rank)
    
    # Interfaces
    os.environ["GLOO_SOCKET_IFNAME"] = "lo" 
    os.environ["USE_FBGEMM"] = "0"
    
    if "FPGA_HOST" not in os.environ:
         os.environ["FPGA_HOST"] = "127.0.0.1"

    os.environ["UDP_MOD_MAX_LEVEL"] = "3"
    os.environ["UDP_MOD_RESPONSE_LEVEL"] = "4"
         
    # Update Collective ID for this iteration
    base_collective_id = int(os.environ.get("UDP_MOD_COLLECTIVE_ID", "1"), 0)
    current_id = base_collective_id + iteration_id
    os.environ["UDP_MOD_COLLECTIVE_ID"] = str(current_id)

    # Tensor Size
    num_chunks = int(os.environ.get("NUM_CHUNKS", "4"))
    tensor_size = num_chunks * 256
    
    # Initialize Gloo
    dist.init_process_group("gloo", rank=rank, world_size=size)
    
    if rank == 0:
        print(f"[Node {rank}] Iter {iteration_id}: ID={hex(current_id)} started for {tensor_size} elements")

    # Setup Deterministic Random Tensors
    # Seed = (Base + Rank + Iteration) to ensure unique but replicable data
    torch.manual_seed(1337 + rank + iteration_id)
    tensor = torch.rand(tensor_size, dtype=torch.float32)

    dist.barrier()
    
    start_time = time.perf_counter()
    dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
    end_time = time.perf_counter()
    
    elapsed = (end_time - start_time) * 1000
    
    # Verify result (Only on Rank 0 for efficiency)
    if rank == 0:
        expected_tensor = torch.zeros(tensor_size, dtype=torch.float32)
        for r in range(size):
            torch.manual_seed(1337 + r + iteration_id)
            expected_tensor.add_(torch.rand(tensor_size, dtype=torch.float32))
        
        max_diff = (tensor - expected_tensor).abs().max().item()
        status = "OK" if max_diff < 1e-4 else "FAIL"
                
        print(f"[Node {rank}] Iter {iteration_id}: Status={status} (MaxDiff={max_diff:.6f}) Time={elapsed:.2f}ms")
        
        # Send timing to main process
        time_queue.put(elapsed)

    dist.destroy_process_group()

if __name__ == "__main__":
    import numpy as np
    multiprocessing.set_start_method("spawn")
    
    print(f"Starting {WORLD_SIZE}-node AllReduce Test for FireSim ({ITERATIONS} iterations)...")
    
    time_queue = multiprocessing.Queue()
    execution_times = []

    for i in range(ITERATIONS):
        # Spawn fresh processes for each iteration
        processes = []
        for rank in range(WORLD_SIZE):
            p = multiprocessing.Process(target=run_node, args=(rank, WORLD_SIZE, i, time_queue))
            p.start()
            processes.append(p)
            
        for p in processes:
            p.join()
        
        # Collect timing from rank 0
        while not time_queue.empty():
            execution_times.append(time_queue.get())
        
        time.sleep(0.05)
    
    # Print statistics
    print("\n" + "="*50)
    print("Test Complete. Statistics:")
    print("="*50)
    if execution_times:
        avg_time = np.mean(execution_times)
        std_time = np.std(execution_times)
        min_time = np.min(execution_times)
        max_time = np.max(execution_times)
        print(f"Total Iterations: {len(execution_times)}")
        print(f"Average Time:     {avg_time:.2f} ms")
        print(f"Std Deviation:    {std_time:.2f} ms")
        print(f"Min Time:         {min_time:.2f} ms")
        print(f"Max Time:         {max_time:.2f} ms")
    else:
        print("No timing data collected.")
    print("="*50)

