#!/bin/bash
# ============================================================
# Runner: Distributed ResNet-18 CIFAR-10 Training — FPGA AllReduce
# ============================================================
# Chunked data-parallel training: 8 workers, each processing
# 1/8th of CIFAR-10, gradient AllReduce split into 44 × 1MB chunks.
#
# CID budget per run:
#   (1 barrier + 44 AllReduces) × 98 batches × epochs
#   10 epochs → ~44,100 CIDs
#   50 epochs → ~220,500 CIDs  (need starting CID chosen carefully)
#
#
# Usage:
#   bash run_train_resnet18.sh [starting_cid] [log_name] [epochs] [batch_size] [optional args...]
#
#   Example (Initial 15 epochs max, saving checkpoint):
#     bash run_train_resnet18.sh 0x0001 logs/resnet_ep1-15 15 64 --save-to checkpoints/resnet_ep15.pth
#
#   Example (Resume for epochs 16-30, loading previous checkpoint):
#     bash run_train_resnet18.sh 0x0100 logs/resnet_ep16-30 15 64 \
#         --resume-from checkpoints/resnet_ep15.pth \
#         --save-to checkpoints/resnet_ep30.pth
#
# ⚠ CID OVERFLOW WARNING:
#   Each batch: 1 barrier + 43 AR chunks = 44 CIDs
#   10 epochs × 98 batches × 44 = ~43,120 CIDs → ends ~0xA8D0 (safe, uint16 max=0xFFFF)
#   50 epochs = ~215,600 CIDs → OVERFLOWS uint16! Do NOT run 50 epochs without checking
#   the CID data type in pair.cc / NIC hardware first.
# ============================================================

# Ensure we run from the script's own directory
cd "$(dirname "$0")"
mkdir -p logs

echo "=== Distributed ResNet-18 Training (FPGA Chunked AllReduce) ==="

# --- FP Format ---
FP_FP32=0x00
FP_BF16=0x01
FP_DLFLOAT=0x02
export UDP_MOD_FP_FORMAT=$FP_FP32

# --- Operation Type ---
OP_SUM=0x05
OP_AVERAGE=0x06
export UDP_MOD_OPERATION=$OP_SUM   # SUM + software /8

# --- Simulation Config ---
export UDP_MOD_DRY_RUN=0
export NUM_CHUNKS=1024              # 1024 chunks × 1KB = 1MB per AllReduce call

# --- Collective ID ---
# Each batch consumes: 1 (barrier) + 44 (AllReduce chunks) = 45 CIDs
# 10 epochs × 98 batches × 45 = ~44,100 CIDs → pick start CID accordingly
# Use a starting CID well past all previous CIFAR-10 runs (which used ~0x0001–0x4630)
export UDP_MOD_COLLECTIVE_ID=${1:-0x5000}

# --- Flow Control ---
export UDP_MOD_MAX_IN_FLIGHT=10
export UDP_MOD_SEND_DELAY_US=50
export UDP_MOD_ITERATION_TIMEOUT=60

# --- Debug ---
export UDP_MOD_LOG_PACKETS=0
export PYTHONUNBUFFERED=1

# --- Cleanup ---
echo "Cleaning up previous processes..."
kill -9 $(pgrep -f "python.*resnet18") 2>/dev/null || true
kill -9 $(pgrep -f "python.*train_resnet") 2>/dev/null || true
kill -9 $(lsof -t -i:29500) 2>/dev/null || true
sleep 1

# --- Run ---
LOG_FILE="${2:-logs/resnet18_$(date +%y_%m_%d_%H%M)}"
EPOCHS="${3:-10}"
BATCH_SIZE="${4:-64}"

echo "Config: FP=${UDP_MOD_FP_FORMAT}, OP=${UDP_MOD_OPERATION}, CID=${UDP_MOD_COLLECTIVE_ID}"
echo "Epochs=${EPOCHS}, Batch=${BATCH_SIZE}"
echo "Log: ${LOG_FILE}"
echo ""

python3 train_resnet18_distributed.py \
    --epochs "$EPOCHS" \
    --batch-size "$BATCH_SIZE" \
    "${@:5}" \
    2>&1 | tee "$LOG_FILE"

echo ""
echo "Training complete. Log saved to: $LOG_FILE"
