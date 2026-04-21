#!/usr/bin/env python3
"""
AllReduce test with configurable FP format (FP32, BF16, or DLFloat).

Usage:
    # FP32 (default)
    export UDP_MOD_FP_FORMAT=0x00
    python3 test_allreduce_fp_formats.py
    
    # BF16
    export UDP_MOD_FP_FORMAT=0x01
    python3 test_allreduce_fp_formats.py
    
    # DLFloat
    export UDP_MOD_FP_FORMAT=0x02
    python3 test_allreduce_fp_formats.py
"""

import os
import struct
import torch
import torch.distributed as dist
import multiprocessing
import time
import socket

# Configuration
MASTER_ADDR = "127.0.0.1"
MASTER_PORT = "29500"
WORLD_SIZE = 8
ITERATIONS = int(os.environ.get("UDP_MOD_ITERATIONS", "64"))

# FP Format constants
FP_FORMAT_FP32 = 0x00
FP_FORMAT_BF16 = 0x01
FP_FORMAT_DLFLOAT = 0x02

# Operation type constants (matches hardware RecursiveDoublingWithDMA)
OP_TYPE_SUM = 0x05
OP_TYPE_AVERAGE = 0x06

# Elements per chunk by format (all use 1KB payloads)
ELEMENTS_PER_CHUNK = {
    FP_FORMAT_FP32: 256,      # 256 * 4 bytes = 1024
    FP_FORMAT_BF16: 512,      # 512 * 2 bytes = 1024
    FP_FORMAT_DLFLOAT: 512,   # 512 * 2 bytes = 1024
}

# PyTorch dtype by format
# Note: DLFloat uses bfloat16 as wire format - Gloo doesn't support int16 all_reduce,
# but we can bit-cast DLFloat uint16 bits to bfloat16 since they're both 16-bit.
# Hardware interprets raw bytes as DLFloat based on fp_format=0x02 header.
TORCH_DTYPES = {
    FP_FORMAT_FP32: torch.float32,
    FP_FORMAT_BF16: torch.bfloat16,
    FP_FORMAT_DLFLOAT: torch.bfloat16,  # Bit-cast DLFloat to bfloat16 for wire
}

FORMAT_NAMES = {
    FP_FORMAT_FP32: "FP32",
    FP_FORMAT_BF16: "BF16",
    FP_FORMAT_DLFLOAT: "DLFloat",
}

# ============================================================================
# DLFloat Conversion Functions (matching hardware MiniFloatAdder.scala)
# DLFloat: 1 sign, 6 exp (bias=31), 9 mantissa, RNU rounding, no subnormals
# ============================================================================

def fp32_to_dlfloat(f: float) -> int:
    """Convert FP32 to DLFloat (16-bit). Returns raw uint16 bits."""
    bits = struct.unpack('>I', struct.pack('>f', f))[0]
    
    sign = (bits >> 31) & 0x1
    exp32 = (bits >> 23) & 0xFF
    mant32 = bits & 0x7FFFFF
    
    # Zero
    if exp32 == 0 and mant32 == 0:
        return 0x0000
    # FP32 subnormal -> DLFloat zero
    if exp32 == 0:
        return 0x0000
    # FP32 Inf/NaN -> DLFloat NaN-Inf (e=63, m=511)
    if exp32 == 255:
        return 0x7FFF
    
    # Convert exponent: FP32 bias=127, DLFloat bias=31
    exp_unbiased = exp32 - 127
    exp_dlf = exp_unbiased + 31
    
    # Underflow
    if exp_dlf <= 0:
        return 0x0000
    # Overflow -> max normal (e=63, m=510)
    if exp_dlf >= 63:
        return (sign << 15) | 0x7FFE
    
    # RNU rounding: add 1 if guard bit (bit 13) is set
    guard = (mant32 >> 13) & 0x1
    mant_dlf = (mant32 >> 14) + guard
    
    # Handle mantissa overflow from rounding
    if mant_dlf > 0x1FF:
        mant_dlf = 0
        exp_dlf += 1
        if exp_dlf >= 63:
            return (sign << 15) | 0x7FFE
    
    return (sign << 15) | (exp_dlf << 9) | mant_dlf


def dlfloat_to_fp32(dlf: int) -> float:
    """Convert DLFloat (16-bit) to FP32. Takes raw uint16 bits."""
    # NaN-Inf
    if dlf == 0x7FFF or dlf == 0xFFFF:
        return float('inf')
    # Zero
    if dlf == 0x0000 or dlf == 0x8000:
        return 0.0
    
    sign = (dlf >> 15) & 0x1
    exp_dlf = (dlf >> 9) & 0x3F
    mant_dlf = dlf & 0x1FF
    
    # Convert exponent: DLFloat bias=31, FP32 bias=127
    exp_unbiased = exp_dlf - 31
    exp32 = exp_unbiased + 127
    
    if exp32 <= 0:
        return -0.0 if sign else 0.0
    if exp32 >= 255:
        return float('-inf') if sign else float('inf')
    
    # Extend mantissa from 9 to 23 bits
    mant32 = mant_dlf << 14
    
    bits = (sign << 31) | (exp32 << 23) | mant32
    return struct.unpack('>f', struct.pack('>I', bits))[0]


def fp32_tensor_to_dlfloat_bytes(tensor: torch.Tensor) -> bytes:
    """Convert FP32 tensor to DLFloat bytes for wire transmission."""
    result = bytearray()
    for val in tensor.flatten().tolist():
        dlf = fp32_to_dlfloat(val)
        result.extend(struct.pack('<H', dlf))  # Little-endian uint16
    return bytes(result)


def dlfloat_bytes_to_fp32_tensor(data: bytes, shape: tuple) -> torch.Tensor:
    """Convert DLFloat bytes back to FP32 tensor."""
    num_elements = len(data) // 2
    values = []
    for i in range(num_elements):
        dlf = struct.unpack('<H', data[i*2:(i+1)*2])[0]
        values.append(dlfloat_to_fp32(dlf))
    return torch.tensor(values, dtype=torch.float32).reshape(shape)


def fp32_roundtrip_dlfloat(val: float) -> float:
    """Round-trip FP32 through DLFloat to capture precision loss."""
    return dlfloat_to_fp32(fp32_to_dlfloat(val))


def get_fp_format():
    """Parse UDP_MOD_FP_FORMAT from environment."""
    env_val = os.environ.get("UDP_MOD_FP_FORMAT", "0x00")
    try:
        return int(env_val, 0)
    except ValueError:
        return FP_FORMAT_FP32


def get_op_type():
    """Parse UDP_MOD_OPERATION from environment. Default: SUM (0x05)."""
    env_val = os.environ.get("UDP_MOD_OPERATION", "0x05")
    try:
        return int(env_val, 0)
    except ValueError:
        return OP_TYPE_SUM


def run_node(rank, size, iteration_id, time_queue, fp_format, op_type):
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

    # Get format-specific parameters
    dtype = TORCH_DTYPES.get(fp_format, torch.float32)
    elements_per_chunk = ELEMENTS_PER_CHUNK.get(fp_format, 256)
    format_name = FORMAT_NAMES.get(fp_format, "Unknown")

    # Tensor Size
    num_chunks = int(os.environ.get("NUM_CHUNKS", "4"))
    tensor_size = num_chunks * elements_per_chunk
    
    # Initialize Gloo
    dist.init_process_group("gloo", rank=rank, world_size=size)
    
    op_name = "AVERAGE" if op_type == OP_TYPE_AVERAGE else "SUM"
    if rank == 0 and iteration_id == 0:
        print(f"[Config] Format={format_name} (0x{fp_format:02x}), Op={op_name}, Dtype={dtype}, "
              f"Elements/Chunk={elements_per_chunk}, TensorSize={tensor_size}")

    if rank == 0:
        print(f"[Node {rank}] Iter {iteration_id}: ID={hex(current_id)} started")

    # Setup Deterministic Random Tensors
    # Seed = (Base + Rank + Iteration) to ensure unique but replicable data
    torch.manual_seed(1337 + rank + iteration_id)
    
    if fp_format == FP_FORMAT_DLFLOAT:
        # DLFloat: Generate FP32 random values, convert to DLFloat bits
        fp32_tensor = torch.rand(tensor_size, dtype=torch.float32)
        # Convert each FP32 value to DLFloat raw bits (as int16)
        dlfloat_bits = torch.tensor([fp32_to_dlfloat(v) for v in fp32_tensor.tolist()], dtype=torch.int16)
        # Bit-cast int16 to bfloat16 for Gloo transmission (same raw bytes)
        tensor = dlfloat_bits.view(torch.bfloat16)
    else:
        tensor = torch.rand(tensor_size, dtype=dtype)

    dist.barrier()
    
    start_time = time.perf_counter()
    # Note: Hardware handles AVG internally; Gloo always does SUM on wire
    dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
    end_time = time.perf_counter()
    
    elapsed = (end_time - start_time) * 1000
    
    # For DLFloat: convert result back to FP32 for verification
    if fp_format == FP_FORMAT_DLFLOAT:
        # Bit-cast bfloat16 back to int16 to get DLFloat bits
        result_bits = tensor.view(torch.int16)
        result_fp32 = torch.tensor([dlfloat_to_fp32(int(v) & 0xFFFF) for v in result_bits.tolist()], dtype=torch.float32)
    else:
        result_fp32 = tensor
    
    # Verify result (Only on Rank 0 for efficiency)
    if rank == 0:
        # Generate all rank tensors
        # For DLFloat: generate in FP32 and round-trip to capture initial precision loss
        rank_tensors = []
        for r in range(size):
            torch.manual_seed(1337 + r + iteration_id)
            if fp_format == FP_FORMAT_DLFLOAT:
                # Generate FP32 and round-trip through DLFloat to match input precision
                fp32_vals = torch.rand(tensor_size, dtype=torch.float32)
                rank_tensors.append(torch.tensor([fp32_roundtrip_dlfloat(v) for v in fp32_vals.tolist()], dtype=torch.float32))
            else:
                rank_tensors.append(torch.rand(tensor_size, dtype=dtype))
        
        # Compute expected using recursive doubling order to match hardware
        # Hardware does: Level 0: (0+1), (2+3), (4+5), (6+7)
        #                Level 1: (0+1)+(2+3), (4+5)+(6+7)
        #                Level 2: ((0+1)+(2+3)) + ((4+5)+(6+7))
        # For DLFloat: simulate precision loss at each addition level
        def recursive_doubling_sum(tensors, apply_precision_loss=False):
            if len(tensors) == 1:
                return tensors[0]
            # Pair up and sum
            pairs = []
            for i in range(0, len(tensors), 2):
                if i + 1 < len(tensors):
                    result = tensors[i] + tensors[i + 1]
                    if apply_precision_loss:
                        # Simulate DLFloat precision loss after each addition
                        result = torch.tensor([fp32_roundtrip_dlfloat(v) for v in result.tolist()], dtype=torch.float32)
                    pairs.append(result)
                else:
                    pairs.append(tensors[i])
            return recursive_doubling_sum(pairs, apply_precision_loss)
        
        is_dlfloat = (fp_format == FP_FORMAT_DLFLOAT)
        expected_tensor = recursive_doubling_sum(rank_tensors, apply_precision_loss=is_dlfloat)
        
        # For AVERAGE: divide by world_size (hardware does this at final level)
        if op_type == OP_TYPE_AVERAGE:
            expected_tensor = expected_tensor / size
            if is_dlfloat:
                # Apply final DLFloat precision loss after division
                expected_tensor = torch.tensor([fp32_roundtrip_dlfloat(v) for v in expected_tensor.tolist()], dtype=torch.float32)
        
        max_diff = (result_fp32 - expected_tensor).abs().max().item()
        
        # Tolerance depends on format (16-bit formats have less precision)
        if fp_format == FP_FORMAT_BF16:
            tolerance = 1e-2
        elif fp_format == FP_FORMAT_DLFLOAT:
            tolerance = 1e-2  # DLFloat has 9-bit mantissa, similar precision to BF16
        else:
            tolerance = 1e-4
        status = "OK" if max_diff < tolerance else "FAIL"
                
        print(f"[Node {rank}] Iter {iteration_id}: Status={status} "
              f"(MaxDiff={max_diff:.6f}, Tol={tolerance}) Time={elapsed:.2f}ms")
        
        # Send timing to main process
        time_queue.put(elapsed)

    dist.destroy_process_group()


if __name__ == "__main__":
    import numpy as np
    multiprocessing.set_start_method("spawn")
    
    fp_format = get_fp_format()
    op_type = get_op_type()
    format_name = FORMAT_NAMES.get(fp_format, "Unknown")
    op_name = "AVERAGE" if op_type == OP_TYPE_AVERAGE else "SUM"
    
    print(f"Starting {WORLD_SIZE}-node AllReduce Test ({format_name} format, {op_name} op, {ITERATIONS} iterations)...")
    
    if fp_format not in TORCH_DTYPES:
        print(f"ERROR: Unknown fp_format 0x{fp_format:02x}. Supported: 0x00 (FP32), 0x01 (BF16), 0x02 (DLFloat)")
        exit(1)
    
    time_queue = multiprocessing.Queue()
    execution_times = []
    
    # Per-iteration timeout (seconds) - if no response, iteration is stuck
    ITERATION_TIMEOUT = int(os.environ.get("UDP_MOD_ITERATION_TIMEOUT", "120"))

    for i in range(ITERATIONS):
        # Spawn fresh processes for each iteration
        processes = []
        for rank in range(WORLD_SIZE):
            p = multiprocessing.Process(target=run_node, args=(rank, WORLD_SIZE, i, time_queue, fp_format, op_type))
            p.start()
            processes.append(p)
        
        # Wait for all processes with timeout
        stuck = False
        for p in processes:
            p.join(timeout=ITERATION_TIMEOUT)
            if p.is_alive():
                stuck = True
        
        if stuck:
            print(f"[ERROR] Iteration {i} STUCK - no response within {ITERATION_TIMEOUT}s. Killing processes...")
            for p in processes:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=2)
                    if p.is_alive():
                        p.kill()
            print(f"[ERROR] Test FAILED due to stuck iteration. Completed {i}/{ITERATIONS} iterations.")
            # Print partial stats if any
            if execution_times:
                print(f"\nPartial Statistics ({len(execution_times)} successful iterations):")
                print(f"Average Time:     {np.mean(execution_times):.2f} ms")
                print(f"Min Time:         {np.min(execution_times):.2f} ms")
                print(f"Max Time:         {np.max(execution_times):.2f} ms")
            exit(1)
        
        # Collect timing from rank 0
        while not time_queue.empty():
            execution_times.append(time_queue.get())
        
        time.sleep(0.05)
    
    # Print statistics
    print("\n" + "="*50)
    print(f"Test Complete ({format_name}). Statistics:")
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

