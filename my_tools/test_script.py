import numpy
import os
import torch
import torch.distributed as dist

os.environ["MASTER_ADDR"] = "192.168.2.170"
os.environ["MASTER_PORT"] = "30001"
os.environ["MASTER_UDP_PORT"] = "30002"
os.environ["WORLD_SIZE"] = "8"
os.environ["GLOO_SOCKET_IFNAME"] = "ens6"
os.environ["NCCL_SOCKET_IFNAME"] = "ens6"
os.environ["NCCL_IB_DISABLE"] = "1"
os.environ["SYNC_UDP"] = "" # Rank 0 waits on a sync packet from the rest before sending a pkt to FPGA for idea response time
os.environ["LOG_SEND_RECV"] = "" # Log time between write-read to and from the FPGA
# os.environ["RANK"] = "0"
print(os.environ)
if os.environ["RANK"] == "0":
    input("Enter to continue")
torch.distributed.init_process_group("gloo", group_name='magramal_gpu')
# torch.distributed.init_process_group("nccl", group_name='magramal_gpu')
torch.cuda.set_device(4 if int(os.environ["RANK"]) % 2 == 0 else 3)
# torch.cuda.device("gpu")

start = torch.cuda.Event(enable_timing=True)
end = torch.cuda.Event(enable_timing=True)

execution_times = []

for i in range(500):
    # tensor = torch.randint(0,100, (2499840,), dtype=torch.int32) # 2499840/256 = 9765 iterations
    tensor = torch.full((256,), int(os.environ["RANK"]), dtype=torch.int32)
    # tensor = torch.range(start=1,end=512, dtype=torch.int32)
    print(tensor)
    tensor = tensor.cuda()
    if i != 0:
        dist.barrier()

    start.record()
    dist.all_reduce(tensor)
    end.record()
    torch.cuda.synchronize()
    print(tensor)
    if i != 0:
        execution_times.append(start.elapsed_time(end))
    print(i, "Elapse time: ", start.elapsed_time(end), "ms")

average = numpy.mean(execution_times)
standard_deviation = numpy.std(execution_times)

print(f'Average execution time: {average} ms')
print(f'Standard deviation of execution times: {standard_deviation} ms')
