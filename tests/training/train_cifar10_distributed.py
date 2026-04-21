#!/usr/bin/env python3
"""
Distributed CIFAR-10 CNN Training using FPGA-Accelerated AllReduce.

Standard data-parallel training: 8 persistent workers, each processing
1/8th of the data with gradient synchronization via hardware AllReduce.

Mirrors real multi-node distributed training — each process acts as an
independent node with its own model, optimizer, and data shard.

NO torchvision dependency — CIFAR-10 is loaded manually.

Usage:
    bash run_train_cifar10.sh
"""

import os
import sys
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

# CIFAR-10 normalization constants
CIFAR10_MEAN = (0.4914, 0.4822, 0.4465)
CIFAR10_STD = (0.2470, 0.2399, 0.2023)


# ============================================================================
# CIFAR-10 Loading (no torchvision)
# ============================================================================

def download_cifar10(root):
    """Download and extract CIFAR-10 if not present."""
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
    """Load a single CIFAR-10 batch file."""
    with open(filepath, "rb") as f:
        batch = pickle.load(f, encoding="bytes")
    data = batch[b"data"]
    labels = batch[b"labels"]
    return data, labels


def load_cifar10(root, train=True):
    """Load CIFAR-10 dataset, returns (images_tensor, labels_tensor)."""
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

    # Reshape to (N, 3, 32, 32) and normalize to [0, 1]
    images = data.reshape(-1, 3, 32, 32).astype(np.float32) / 255.0

    # Normalize with CIFAR-10 mean/std
    for c in range(3):
        images[:, c] = (images[:, c] - CIFAR10_MEAN[c]) / CIFAR10_STD[c]

    return torch.tensor(images), torch.tensor(labels, dtype=torch.long)


class CIFAR10Dataset(torch.utils.data.Dataset):
    """Simple CIFAR-10 dataset wrapper."""
    def __init__(self, images, labels):
        self.images = images
        self.labels = labels

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        return self.images[idx], self.labels[idx]


# ============================================================================
# Model Definition (~137K parameters → ~535 KB in FP32)
# ============================================================================

class SmallCIFAR10CNN(nn.Module):
    """Small CNN for CIFAR-10, designed to fit within 1 MB AllReduce limit."""
    def __init__(self):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(3, 16, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(16, 32, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
        )
        self.classifier = nn.Sequential(
            nn.Linear(32 * 8 * 8, 64),
            nn.ReLU(inplace=True),
            nn.Linear(64, 10),
        )

    def forward(self, x):
        x = self.features(x)
        x = x.view(x.size(0), -1)
        x = self.classifier(x)
        return x


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


# ============================================================================
# Gradient Flattening Utilities
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


def compute_num_chunks(num_params, fp_format):
    elems_per_chunk = ELEMENTS_PER_CHUNK.get(fp_format, 256)
    return math.ceil(num_params / elems_per_chunk)


# ============================================================================
# Persistent Worker — each process acts as a distinct node
# ============================================================================

def train_worker(rank, shared_train_images, shared_train_labels,
                 shared_test_images, shared_test_labels, epoch_queue, args_dict):
    """
    Standard distributed training worker.

    Each worker is an independent node that:
    1. Initializes Gloo ONCE (persistent connection)
    2. Loads its 1/8th shard of the training data
    3. Runs the full training loop (all epochs)
    4. Destroys the process group at the end

    This is exactly how real multi-node training works — the only
    difference is all 8 "nodes" share the same physical machine.
    """

    # --- Environment Setup (same as test_allreduce_fp_formats.py) ---
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

    # Unpack args
    epochs = args_dict["epochs"]
    batch_size = args_dict["batch_size"]
    fp_format = args_dict["fp_format"]
    op_type = args_dict["op_type"]
    num_chunks = args_dict["num_chunks"]
    elems_per_chunk = ELEMENTS_PER_CHUNK.get(fp_format, 256)
    padded_size = num_chunks * elems_per_chunk

    # --- Initialize Gloo ONCE (persistent — no respawn per batch) ---
    dist.init_process_group("gloo", rank=rank, world_size=WORLD_SIZE)

    if rank == 0:
        print(f"  [Rank 0] Gloo initialized. All 8 workers connected.\n")

    # --- Create model (identical seed → identical initial weights on all ranks) ---
    torch.manual_seed(42)
    model = SmallCIFAR10CNN()
    num_params = count_parameters(model)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.9, weight_decay=1e-4)

    # --- Data shard: each rank gets every 8th sample ---
    # rank 0: samples 0, 8, 16, ...
    # rank 1: samples 1, 9, 17, ...
    # This is standard distributed data partition (strided sharding)
    local_train_images = shared_train_images[rank::WORLD_SIZE]
    local_train_labels = shared_train_labels[rank::WORLD_SIZE]

    train_dataset = CIFAR10Dataset(local_train_images, local_train_labels)
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True, num_workers=0)

    if rank == 0:
        print(f"  [Rank 0] Local shard: {len(train_dataset)} training samples "
              f"({len(train_loader)} batches of {batch_size})")

    # --- Training loop ---
    step_counter = 0

    for epoch in range(epochs):
        model.train()
        epoch_loss = 0.0
        epoch_correct = 0
        epoch_total = 0
        epoch_ar_time = 0.0
        num_batches = 0

        for batch_idx, (inputs, targets) in enumerate(train_loader):
            # --- Forward + Backward (local computation) ---
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()

            # --- Flatten gradients and pad to chunk boundary ---
            flat_grads = flatten_gradients(model)
            if flat_grads.numel() < padded_size:
                flat_grads = torch.cat([
                    flat_grads,
                    torch.zeros(padded_size - flat_grads.numel())
                ])

            # --- AllReduce: average gradients across all 8 nodes ---
            # Barrier synchronizes ranks so all enter AllReduce together,
            # reducing measured latency and variance.
            dist.barrier()
            start_time = time.perf_counter()
            dist.all_reduce(flat_grads, op=dist.ReduceOp.SUM)
            end_time = time.perf_counter()
            ar_ms = (end_time - start_time) * 1000

            # --- Diagnostic: check AllReduce output (first 2 batches) ---
            if rank == 0 and epoch == 0 and batch_idx < 2:
                ar_nan = torch.isnan(flat_grads).any().item()
                print(f"  [Debug] Batch {batch_idx}: Post-AR[:5]={flat_grads[:5].tolist()} "
                      f"NaN={ar_nan} AR={ar_ms:.0f}ms")

            # --- Apply averaged gradients ---
            # With hardware AVERAGE: result is already averaged
            # With SUM: divide by WORLD_SIZE in software
            if op_type == OP_TYPE_SUM:
                flat_grads = flat_grads / WORLD_SIZE

            unflatten_gradients(model, flat_grads[:num_params])

            # --- Gradient clipping (standard for distributed training) ---
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)

            optimizer.step()

            # --- Track metrics (all ranks compute independently) ---
            epoch_loss += loss.item()
            _, predicted = outputs.max(1)
            epoch_total += targets.size(0)
            epoch_correct += predicted.eq(targets).sum().item()
            epoch_ar_time += ar_ms
            num_batches += 1

            # --- Progress (rank 0 only) ---
            if rank == 0 and (batch_idx + 1) % 20 == 0:
                print(f"  [Epoch {epoch+1}] Batch {batch_idx+1}/{len(train_loader)}: "
                      f"Loss={loss.item():.4f}, AR={ar_ms:.0f}ms")

            step_counter += 1

        # --- Epoch summary (rank 0 evaluates on test set) ---
        train_acc = 100.0 * epoch_correct / epoch_total if epoch_total > 0 else 0
        avg_ar = epoch_ar_time / num_batches if num_batches > 0 else 0

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
                  f"AvgAR={avg_ar:.0f}ms\n")

            # Send epoch summary to main process (small dict — no deadlock)
            epoch_queue.put({
                "epoch": epoch + 1,
                "train_loss": epoch_loss / num_batches,
                "train_acc": train_acc,
                "test_acc": test_acc,
                "avg_allreduce_ms": avg_ar,
                "batches": num_batches,
            })

    # --- Cleanup (destroy process group at end of training) ---
    dist.destroy_process_group()

    if rank == 0:
        print(f"  [Rank 0] Training complete. Total steps: {step_counter}")


# ============================================================================
# Main — launches 8 persistent workers
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Distributed CIFAR-10 Training on FPGA AllReduce")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()

    multiprocessing.set_start_method("spawn", force=True)

    # --- Config from env ---
    fp_format = int(os.environ.get("UDP_MOD_FP_FORMAT", "0"), 0)
    op_type = int(os.environ.get("UDP_MOD_OPERATION", str(OP_TYPE_AVERAGE)), 0)
    num_chunks = int(os.environ.get("NUM_CHUNKS", "1024"))
    fmt_names = {0: "FP32", 1: "BF16", 2: "DLFloat"}
    elems_per_chunk = ELEMENTS_PER_CHUNK.get(fp_format, 256)
    padded_size = num_chunks * elems_per_chunk

    # --- Download & Load CIFAR-10 ---
    print("Preparing CIFAR-10...")
    download_cifar10(CIFAR10_ROOT)
    train_images, train_labels = load_cifar10(CIFAR10_ROOT, train=True)
    test_images, test_labels = load_cifar10(CIFAR10_ROOT, train=False)
    print(f"  Train: {len(train_labels)} images, Test: {len(test_labels)} images")

    # Move to shared memory so all workers can access without pickling
    train_images.share_memory_()
    train_labels.share_memory_()
    test_images.share_memory_()
    test_labels.share_memory_()

    # --- Model info ---
    torch.manual_seed(42)
    model = SmallCIFAR10CNN()
    num_params = count_parameters(model)
    local_samples = len(train_labels) // WORLD_SIZE
    local_batches = math.ceil(local_samples / args.batch_size)
    del model  # Only needed for param count

    # --- Print config ---
    print(f"\n{'='*60}")
    print(f"Distributed CIFAR-10 Training — FPGA AllReduce")
    print(f"{'='*60}")
    print(f"  Nodes:         {WORLD_SIZE} persistent workers")
    print(f"  Epochs:        {args.epochs}")
    print(f"  Batch size:    {args.batch_size} per node")
    print(f"  Eff. batch:    {args.batch_size * WORLD_SIZE} (across all nodes)")
    print(f"  Data/node:     {local_samples} samples ({local_batches} batches)")
    print(f"  Model:         SmallCIFAR10CNN ({num_params:,} params)")
    print(f"  Grad tensor:   {padded_size} elements ({padded_size*4//1024} KB, {num_chunks} chunks)")
    print(f"  FP Format:     {fmt_names.get(fp_format, '?')} (0x{fp_format:02x})")
    print(f"  Operation:     {'AVERAGE' if op_type == OP_TYPE_AVERAGE else 'SUM'}")
    print(f"  Start CID:     {os.environ.get('UDP_MOD_COLLECTIVE_ID', 'auto')}")
    print(f"{'='*60}\n")

    args_dict = {
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "fp_format": fp_format,
        "op_type": op_type,
        "num_chunks": num_chunks,
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

    # --- Wait for all workers to complete ---
    for p in processes:
        p.join()

    # --- Collect epoch results from rank 0 ---
    epoch_results = []
    while not epoch_queue.empty():
        epoch_results.append(epoch_queue.get())

    # --- Final summary ---
    print(f"\n{'='*60}")
    print(f"Training Complete — Final Results")
    print(f"{'='*60}")
    print(f"{'Epoch':>6} {'Loss':>10} {'TrainAcc':>10} {'TestAcc':>10} {'AllReduce':>12}")
    print(f"{'-'*6:>6} {'-'*10:>10} {'-'*10:>10} {'-'*10:>10} {'-'*12:>12}")
    for r in epoch_results:
        print(f"{r['epoch']:>6} {r['train_loss']:>10.4f} {r['train_acc']:>9.2f}% "
              f"{r['test_acc']:>9.2f}% {r['avg_allreduce_ms']:>10.0f}ms")
    if epoch_results:
        final = epoch_results[-1]
        print(f"\nFinal Test Accuracy: {final['test_acc']:.2f}%")
        print(f"Avg AllReduce Time:  {np.mean([r['avg_allreduce_ms'] for r in epoch_results]):.0f}ms")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
