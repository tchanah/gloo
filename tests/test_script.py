import numpy
import os
import time
import torch
import torch.distributed as dist

os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
os.environ.setdefault("MASTER_PORT", "29500")
os.environ.setdefault("MASTER_UDP_PORT", "30002")
os.environ.setdefault("WORLD_SIZE", "8")
os.environ.setdefault("GLOO_SOCKET_IFNAME", "eno1")
os.environ.setdefault("NCCL_SOCKET_IFNAME", "eno1")
os.environ.setdefault("NCCL_IB_DISABLE", "1")
os.environ.setdefault("UDP_MOD_COLLECTIVE_TYPE", "1")
os.environ.setdefault("UDP_MOD_OPERATION", "5")
os.environ.setdefault("UDP_MOD_MAX_LEVEL", "4")
os.environ.setdefault("UDP_MOD_RESPONSE_LEVEL", "4")
os.environ.setdefault("SYNC_UDP", "")
os.environ.setdefault("LOG_SEND_RECV", "")
# os.environ["RANK"] = "0"
print(os.environ)
if os.environ.get("UDP_MOD_WAIT_FOR_INPUT"):
    input("Enter to continue")
torch.distributed.init_process_group("gloo", group_name='magramal_gpu')
# torch.distributed.init_process_group("nccl", group_name='magramal_gpu')

use_cuda = torch.cuda.is_available()
if use_cuda:
    torch.cuda.set_device(4 if int(os.environ["RANK"]) % 2 == 0 else 3)
# torch.cuda.device("gpu")

if use_cuda:
    start_event = torch.cuda.Event(enable_timing=True)
    end_event = torch.cuda.Event(enable_timing=True)

execution_times = []

for i in range(1):
    # tensor = torch.randint(0,100, (2499840,), dtype=torch.int32) # 2499840/256 = 9765 iterations
    tensor = torch.full((1024,), int(os.environ["RANK"]), dtype=torch.float32)
    # tensor = torch.range(start=1,end=512, dtype=torch.int32)
    print(tensor)
    if use_cuda:
        tensor = tensor.cuda()
    if i != 0:
        dist.barrier()

    if use_cuda:
        start_event.record()
        dist.all_reduce(tensor)
        end_event.record()
        torch.cuda.synchronize()
        elapsed_ms = start_event.elapsed_time(end_event)
    else:
        start_time = time.perf_counter()
        dist.all_reduce(tensor)
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
    print(tensor)
    if i != 0:
        execution_times.append(elapsed_ms)
    print(i, "Elapse time: ", elapsed_ms, "ms")

if execution_times:
    average = numpy.mean(execution_times)
    standard_deviation = numpy.std(execution_times)
else:
    average = 0.0
    standard_deviation = 0.0

print(f'Average execution time: {average} ms')
print(f'Standard deviation of execution times: {standard_deviation} ms')
