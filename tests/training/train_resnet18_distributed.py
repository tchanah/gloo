#!/usr/bin/env python3
"""
Distributed ResNet-18 CIFAR-10 Training using FPGA-Accelerated AllReduce.

Standard data-parallel training: 8 persistent workers, each processing
1/8th of the data with gradient synchronization via CHUNKED hardware AllReduce.

ResNet-18 has ~11.2M parameters (44 MB in FP32). Since the FPGA AllReduce
is limited to 1MB per call (1024 chunks × 1KB), gradients are split into
1MB chunks and AllReduced sequentially — exactly as real DDP bucket AllReduce works.

44 AllReduce calls per batch × CID auto-increment = strong CID increment test.

NO torchvision dependency — CIFAR-10 is loaded manually.

Usage:
    bash run_train_resnet18.sh
"""

import os
import time
import argparse
import math
import pickle
import tarfile
import urllib.request
import torch
import torch.nn as nn
import torch.optim as optim
import torch.distributed as dist
import multiprocessing
import numpy as np

# ============================================================================
# Configuration
# ============================================================================

MASTER_ADDR = "127.0.0.1"
MASTER_PORT = "29500"
WORLD_SIZE = 8

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CIFAR10_ROOT = os.path.join(SCRIPT_DIR, "data")

FP_FORMAT_FP32 = 0x00
FP_FORMAT_BF16 = 0x01
FP_FORMAT_DLFLOAT = 0x02

ELEMENTS_PER_CHUNK = {
    FP_FORMAT_FP32: 256,
    FP_FORMAT_BF16: 512,
    FP_FORMAT_DLFLOAT: 512,
}

OP_TYPE_SUM = 0x05
OP_TYPE_AVERAGE = 0x06

# CIFAR-10 normalization
CIFAR10_MEAN = (0.4914, 0.4822, 0.4465)
CIFAR10_STD = (0.2470, 0.2399, 0.2023)


# ============================================================================
# CIFAR-10 Loading (identical to CIFAR-10 script — no torchvision)
# ============================================================================

def download_cifar10(root):
    cifar_dir = os.path.join(root, "cifar-10-batches-py")
    if os.path.exists(cifar_dir):
        return
    os.makedirs(root, exist_ok=True)
    url = "https://www.cs.toronto.edu/~kriz/cifar-10-python.tar.gz"
    tar_path = os.path.join(root, "cifar-10-python.tar.gz")
    print(f"  Downloading CIFAR-10 from {url}...")
    urllib.request.urlretrieve(url, tar_path)
    print(f"  Extracting...")
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(path=root)
    os.remove(tar_path)
    print(f"  Done.")


def load_cifar10_batch(filepath):
    with open(filepath, "rb") as f:
        batch = pickle.load(f, encoding="bytes")
    return batch[b"data"], batch[b"labels"]


def load_cifar10(root, train=True):
    cifar_dir = os.path.join(root, "cifar-10-batches-py")
    if train:
        all_data, all_labels = [], []
        for i in range(1, 6):
            data, labels = load_cifar10_batch(os.path.join(cifar_dir, f"data_batch_{i}"))
            all_data.append(data)
            all_labels.extend(labels)
        data = np.concatenate(all_data, axis=0)
        labels = all_labels
    else:
        data, labels = load_cifar10_batch(os.path.join(cifar_dir, "test_batch"))

    images = data.reshape(-1, 3, 32, 32).astype(np.float32) / 255.0
    for c in range(3):
        images[:, c] = (images[:, c] - CIFAR10_MEAN[c]) / CIFAR10_STD[c]
    return torch.tensor(images), torch.tensor(labels, dtype=torch.long)


class CIFAR10Dataset(torch.utils.data.Dataset):
    def __init__(self, images, labels):
        self.images = images
        self.labels = labels

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        return self.images[idx], self.labels[idx]


# ============================================================================
# ResNet-18 for CIFAR-10 (32×32 input — modified stem, no maxpool)
# ============================================================================

class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_planes, planes, stride=1):
        super().__init__()
        self.conv1 = nn.Conv2d(in_planes, planes, 3, stride=stride, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, 3, stride=1, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, planes, 1, stride=stride, bias=False),
                nn.BatchNorm2d(planes),
            )

    def forward(self, x):
        out = torch.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out += self.shortcut(x)
        return torch.relu(out)


class ResNet18CIFAR(nn.Module):
    """
    ResNet-18 adapted for CIFAR-10 (32×32 input).
    Modified stem: 3×3 conv, stride=1 (no 7×7 or maxpool — standard for CIFAR).
    ~11.17M parameters, ~43.5 MB in FP32.
    """
    def __init__(self, num_classes=10):
        super().__init__()
        # CIFAR stem: 3×3 conv, stride 1 (not 7×7, stride 2 + maxpool)
        self.stem = nn.Sequential(
            nn.Conv2d(3, 64, 3, stride=1, padding=1, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
        )
        self.layer1 = self._make_layer(64,  64,  2, stride=1)
        self.layer2 = self._make_layer(64,  128, 2, stride=2)
        self.layer3 = self._make_layer(128, 256, 2, stride=2)
        self.layer4 = self._make_layer(256, 512, 2, stride=2)
        self.avgpool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Linear(512, num_classes)

    def _make_layer(self, in_planes, planes, num_blocks, stride):
        layers = [BasicBlock(in_planes, planes, stride)]
        for _ in range(1, num_blocks):
            layers.append(BasicBlock(planes, planes, 1))
        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.stem(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        x = self.avgpool(x)
        return self.fc(x.view(x.size(0), -1))


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


# ============================================================================
# Gradient Utilities — same as CIFAR-10 script
# ============================================================================

def flatten_gradients(model):
    grads = []
    for p in model.parameters():
        if p.grad is not None:
            grads.append(p.grad.data.view(-1))
        else:
            grads.append(torch.zeros(p.numel()))
    return torch.cat(grads)


def unflatten_gradients(model, flat_grads):
    offset = 0
    for p in model.parameters():
        numel = p.numel()
        if p.grad is not None:
            p.grad.data.copy_(flat_grads[offset:offset + numel].view_as(p.data))
        offset += numel


# ============================================================================
# Chunked AllReduce — splits gradient tensor into 1MB chunks
# Each chunk is a separate AllReduce call (= one CID increment per chunk)
# ============================================================================

def chunked_allreduce(flat_grads, chunk_size, op_type, rank=None):
    """
    AllReduce the gradient tensor in 1MB (chunk_size elements) slices.

    This mirrors DDP bucket AllReduce behavior and exercises the FPGA
    CID auto-increment across many sequential calls per training step.

    Returns (total_ar_ms, num_chunks_sent)
    """
    total_ms = 0.0
    num_chunks = 0
    n = flat_grads.numel()

    for start in range(0, n, chunk_size):
        end = min(start + chunk_size, n)
        chunk = flat_grads[start:end].clone()

        # Pad to full chunk size for FPGA (FPGA expects fixed-size packets)
        if len(chunk) < chunk_size:
            pad = torch.zeros(chunk_size - len(chunk))
            chunk = torch.cat([chunk, pad])

        t0 = time.perf_counter()
        dist.all_reduce(chunk, op=dist.ReduceOp.SUM)
        
        chunk_time = (time.perf_counter() - t0) * 1000
        total_ms += chunk_time

        # Write averaged result back (unpad to original slice size)
        result = chunk[:end - start]
        if op_type == OP_TYPE_SUM:
            result = result / WORLD_SIZE
        flat_grads[start:end] = result
        num_chunks += 1

    return total_ms, num_chunks


# ============================================================================
# Persistent Worker — each process acts as a distinct node
# ============================================================================

def train_worker(rank, shared_train_images, shared_train_labels,
                 shared_test_images, shared_test_labels, epoch_queue, args_dict):
    """
    ResNet-18 distributed training worker.

    Key difference from CIFAR-10 script:
    - AllReduce is chunked (44 calls per batch, 1MB each)
    - Uses barrier once per batch (before the chunk loop)
    - Each chunk auto-increments CID in pair.cc
    """

    # --- Environment Setup ---
    os.environ["MASTER_ADDR"] = MASTER_ADDR
    os.environ["MASTER_PORT"] = MASTER_PORT
    os.environ["WORLD_SIZE"] = str(WORLD_SIZE)
    os.environ["RANK"] = str(rank)
    os.environ["GLOO_SOCKET_IFNAME"] = "lo"
    os.environ["USE_FBGEMM"] = "0"

    # CRITICAL: Prevent 8 processes from spawning 64 threads EACH, which locks up the CPU
    os.environ["OMP_NUM_THREADS"] = "2"
    os.environ["MKL_NUM_THREADS"] = "2"

    if "FPGA_HOST" not in os.environ:
        os.environ["FPGA_HOST"] = "127.0.0.1"

    os.environ["UDP_MOD_MAX_LEVEL"] = "3"
    os.environ["UDP_MOD_RESPONSE_LEVEL"] = "4"

    # Unpack args
    epochs = args_dict["epochs"]
    batch_size = args_dict["batch_size"]
    fp_format = args_dict["fp_format"]
    op_type = args_dict["op_type"]
    num_chunks_per_ar = args_dict["num_chunks_per_ar"]  # chunks per 1MB AllReduce
    elems_per_chunk = ELEMENTS_PER_CHUNK.get(fp_format, 256)
    chunk_size = num_chunks_per_ar * elems_per_chunk   # elements per AllReduce call

    # --- Initialize Gloo ONCE ---
    dist.init_process_group("gloo", rank=rank, world_size=WORLD_SIZE)

    if rank == 0:
        print(f"  [Rank 0] Gloo initialized. All 8 workers connected.\n", flush=True)

    # --- Create model (identical seed → identical initial weights) ---
    torch.manual_seed(42)
    model = ResNet18CIFAR()
    
    # --- Resume Checkpoint ---
    checkpoint = None
    if args_dict["resume_from"]:
        if rank == 0:
            print(f"  [Rank 0] Resuming weights from {args_dict['resume_from']}...", flush=True)
        checkpoint = torch.load(args_dict["resume_from"], map_location="cpu")
        model.load_state_dict(checkpoint["model_state_dict"])
    
    num_params = count_parameters(model)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.9, weight_decay=1e-4)

    if checkpoint and "optimizer_state_dict" in checkpoint:
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        if rank == 0:
            print("  [Rank 0] Optimizer state restored.", flush=True)

    # --- Data shard: strided (identical partitioning as CIFAR-10 script) ---
    local_train_images = shared_train_images[rank::WORLD_SIZE]
    local_train_labels = shared_train_labels[rank::WORLD_SIZE]

    train_dataset = CIFAR10Dataset(local_train_images, local_train_labels)
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True, num_workers=0)

    # Compute how many AllReduce calls per batch
    num_ar_calls = math.ceil(num_params / chunk_size)

    if rank == 0:
        print(f"  [Rank 0] Local shard: {len(train_dataset)} training samples "
              f"({len(train_loader)} batches of {batch_size})")
        print(f"  [Rank 0] Gradient AllReduce: {num_params:,} params → "
              f"{num_ar_calls} chunks × {chunk_size} elements each\n")

    # --- Training loop ---
    step_counter = 0

    for epoch in range(epochs):
        model.train()
        epoch_loss = 0.0
        epoch_correct = 0
        epoch_total = 0
        epoch_ar_time = 0.0
        epoch_ar_calls = 0
        num_batches = 0

        for batch_idx, (inputs, targets) in enumerate(train_loader):
            # --- Forward + Backward ---
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()

            # --- Flatten gradients ---
            flat_grads = flatten_gradients(model)

            # --- Barrier: sync all ranks before chunked AllReduce loop ---
            dist.barrier()
            
            # --- Chunked AllReduce (one call per 1MB chunk) ---
            ar_ms, n_chunks = chunked_allreduce(flat_grads, chunk_size, op_type, rank=rank)

            # --- Diagnostic: first 2 batches of epoch 0 ---
            if rank == 0 and epoch == 0 and batch_idx < 2:
                ar_nan = torch.isnan(flat_grads).any().item()
                print(f"  [Debug] Batch {batch_idx}: {n_chunks} AR calls, "
                      f"Total AR={ar_ms:.0f}ms, NaN={ar_nan}")

            # --- Gradient clipping + optimizer step ---
            unflatten_gradients(model, flat_grads[:num_params])
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()

            # --- Track metrics ---
            epoch_loss += loss.item()
            _, predicted = outputs.max(1)
            epoch_total += targets.size(0)
            epoch_correct += predicted.eq(targets).sum().item()
            epoch_ar_time += ar_ms
            epoch_ar_calls += n_chunks
            num_batches += 1

            # --- Progress (rank 0 only) ---
            if rank == 0:
                avg_chunk_ms = ar_ms / n_chunks if n_chunks else 0
                print(f"  [Epoch {epoch+1}] Batch {batch_idx+1}/{len(train_loader)}: "
                      f"Loss={loss.item():.4f}, AR={ar_ms:.0f}ms ({n_chunks} chunks, "
                      f"{avg_chunk_ms:.0f}ms/chunk)")

            step_counter += 1

        # --- Epoch summary ---
        train_acc = 100.0 * epoch_correct / epoch_total if epoch_total > 0 else 0
        avg_ar = epoch_ar_time / num_batches if num_batches > 0 else 0
        avg_ar_per_chunk = epoch_ar_time / epoch_ar_calls if epoch_ar_calls > 0 else 0

        test_acc = 0.0
        if rank == 0:
            model.eval()
            test_correct = 0
            test_total = 0
            test_dataset = CIFAR10Dataset(shared_test_images, shared_test_labels)
            test_loader = torch.utils.data.DataLoader(
                test_dataset, batch_size=batch_size, shuffle=False, num_workers=0)
            with torch.no_grad():
                for inputs, targets in test_loader:
                    outputs = model(inputs)
                    _, predicted = outputs.max(1)
                    test_total += targets.size(0)
                    test_correct += predicted.eq(targets).sum().item()
            test_acc = 100.0 * test_correct / test_total

            print(f"\n[Epoch {epoch+1}/{epochs}] "
                  f"Loss={epoch_loss/num_batches:.4f}, "
                  f"TrainAcc={train_acc:.2f}%, TestAcc={test_acc:.2f}%, "
                  f"TotalAR={avg_ar:.0f}ms ({avg_ar_per_chunk:.0f}ms/chunk)\n")

            epoch_queue.put({
                "epoch": epoch + 1,
                "train_loss": epoch_loss / num_batches,
                "train_acc": train_acc,
                "test_acc": test_acc,
                "avg_total_ar_ms": avg_ar,
                "avg_per_chunk_ms": avg_ar_per_chunk,
                "ar_calls_per_batch": epoch_ar_calls // num_batches if num_batches else 0,
                "batches": num_batches,
            })

    # --- Save Checkpoint ---
    if rank == 0 and args_dict["save_to"]:
        os.makedirs(os.path.dirname(os.path.abspath(args_dict["save_to"])), exist_ok=True)
        print(f"\n  [Rank 0] Saving checkpoint to {args_dict['save_to']}...", flush=True)
        torch.save({
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
        }, args_dict["save_to"])

    # --- Cleanup ---
    dist.destroy_process_group()

    if rank == 0:
        print(f"  [Rank 0] Training complete. Total steps: {step_counter}")


# ============================================================================
# Main — launches 8 persistent workers
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Distributed ResNet-18 CIFAR-10 Training on FPGA AllReduce")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--resume-from", type=str, default="", help="Path to checkpoint to resume from")
    parser.add_argument("--save-to", type=str, default="", help="Path to save checkpoint at the end")
    args = parser.parse_args()

    multiprocessing.set_start_method("spawn", force=True)

    # --- Config from env ---
    fp_format = int(os.environ.get("UDP_MOD_FP_FORMAT", "0"), 0)
    op_type = int(os.environ.get("UDP_MOD_OPERATION", str(OP_TYPE_SUM)), 0)
    num_chunks_per_ar = int(os.environ.get("NUM_CHUNKS", "1024"))
    fmt_names = {0: "FP32", 1: "BF16", 2: "DLFloat"}
    elems_per_chunk = ELEMENTS_PER_CHUNK.get(fp_format, 256)
    chunk_size = num_chunks_per_ar * elems_per_chunk

    # --- Download & Load CIFAR-10 ---
    print("Preparing CIFAR-10...")
    download_cifar10(CIFAR10_ROOT)
    train_images, train_labels = load_cifar10(CIFAR10_ROOT, train=True)
    test_images, test_labels = load_cifar10(CIFAR10_ROOT, train=False)
    print(f"  Train: {len(train_labels)} images, Test: {len(test_labels)} images")

    # Shared memory — no pickling
    train_images.share_memory_()
    train_labels.share_memory_()
    test_images.share_memory_()
    test_labels.share_memory_()

    # --- Model info ---
    torch.manual_seed(42)
    model = ResNet18CIFAR()
    num_params = count_parameters(model)
    num_ar_calls = math.ceil(num_params / chunk_size)
    local_samples = len(train_labels) // WORLD_SIZE
    local_batches = math.ceil(local_samples / args.batch_size)
    del model

    # CIDs consumed per batch: 1 barrier + num_ar_calls AllReduces
    cids_per_batch = 1 + num_ar_calls
    cids_per_epoch = cids_per_batch * local_batches
    cids_total = cids_per_epoch * args.epochs

    # --- Print config ---
    print(f"\n{'='*65}")
    print(f"Distributed ResNet-18 CIFAR-10 Training — FPGA AllReduce (Chunked)")
    print(f"{'='*65}")
    print(f"  Nodes:           {WORLD_SIZE} persistent workers")
    print(f"  Epochs:          {args.epochs}")
    print(f"  Batch size:      {args.batch_size} per node, {args.batch_size * WORLD_SIZE} effective")
    print(f"  Data/node:       {local_samples} samples ({local_batches} batches)")
    print(f"  Model:           ResNet-18 CIFAR ({num_params:,} params, "
          f"{num_params*4//1024//1024:.1f} MB)")
    print(f"  AllReduce:       {chunk_size} elements/call ({chunk_size*4//1024} KB), "
          f"{num_ar_calls} calls/batch")
    print(f"  FP Format:       {fmt_names.get(fp_format, '?')} (0x{fp_format:02x})")
    print(f"  Operation:       {'AVERAGE' if op_type == OP_TYPE_AVERAGE else 'SUM'}")
    print(f"  CID budget:      ~{cids_total} total "
          f"({cids_per_batch}/batch × {local_batches} batches × {args.epochs} epochs)")
    print(f"  Start CID:       {os.environ.get('UDP_MOD_COLLECTIVE_ID', 'auto')}")
    print(f"{'='*65}\n")

    args_dict = {
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "fp_format": fp_format,
        "op_type": op_type,
        "num_chunks_per_ar": num_chunks_per_ar,
        "resume_from": args.resume_from,
        "save_to": args.save_to,
    }

    epoch_queue = multiprocessing.Queue()

    # --- Spawn 8 persistent workers ---
    print("Spawning 8 worker processes...\n")
    processes = []
    for rank in range(WORLD_SIZE):
        p = multiprocessing.Process(
            target=train_worker,
            args=(rank, train_images, train_labels,
                  test_images, test_labels, epoch_queue, args_dict))
        p.start()
        processes.append(p)

    for p in processes:
        p.join()

    # --- Collect & print results ---
    epoch_results = []
    while not epoch_queue.empty():
        epoch_results.append(epoch_queue.get())

    print(f"\n{'='*65}")
    print(f"Training Complete — Final Results")
    print(f"{'='*65}")
    print(f"{'Epoch':>6} {'Loss':>10} {'TrainAcc':>10} {'TestAcc':>10} "
          f"{'TotalAR':>10} {'PerChunk':>10}")
    print(f"{'-'*6:>6} {'-'*10:>10} {'-'*10:>10} {'-'*10:>10} "
          f"{'-'*10:>10} {'-'*10:>10}")
    for r in epoch_results:
        print(f"{r['epoch']:>6} {r['train_loss']:>10.4f} {r['train_acc']:>9.2f}% "
              f"{r['test_acc']:>9.2f}% {r['avg_total_ar_ms']:>9.0f}ms "
              f"{r['avg_per_chunk_ms']:>9.0f}ms")
    if epoch_results:
        final = epoch_results[-1]
        print(f"\nFinal Test Accuracy:    {final['test_acc']:.2f}%")
        print(f"AR calls per batch:     {final['ar_calls_per_batch']}")
        print(f"Avg per-chunk AR time:  "
              f"{np.mean([r['avg_per_chunk_ms'] for r in epoch_results]):.0f}ms")
        print(f"Avg total AR per batch: "
              f"{np.mean([r['avg_total_ar_ms'] for r in epoch_results]):.0f}ms")

        # --- Calculate and Print Final CID ---
        start_cid_str = os.environ.get('UDP_MOD_COLLECTIVE_ID', 'auto')
        if start_cid_str != 'auto':
            try:
                start_cid = int(start_cid_str, 0)
                epochs_run = len(epoch_results)
                cids_used = epochs_run * local_batches * cids_per_batch
                final_cid = start_cid + cids_used - 1
                next_safe_cid = final_cid + 1
                print(f"\nFinal Expected CID:     0x{final_cid:04X} ({final_cid})")
                print(f"Next SAFE Start CID:    0x{next_safe_cid:04X} ({next_safe_cid})")
            except ValueError:
                pass
                
    print(f"{'='*65}")


if __name__ == "__main__":
    main()
