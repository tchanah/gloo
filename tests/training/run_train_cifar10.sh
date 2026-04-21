#!/bin/bash
# ============================================================
# Runner: Distributed CIFAR-10 Training on FPGA AllReduce
# ============================================================
# Persistent data-parallel training: 8 workers, each processing
# 1/8th of CIFAR-10, synchronized via FPGA AllReduce.
#
# Usage:
#   bash run_train_cifar10.sh [log_name] [epochs] [batch_size]
# ============================================================

# Ensure we run from the script's own directory
cd "$(dirname "$0")"
mkdir -p logs

echo "=== Distributed CIFAR-10 Training (FPGA AllReduce) ==="

# --- FP Format ---
FP_FP32=0x00
FP_BF16=0x01
FP_DLFLOAT=0x02
export UDP_MOD_FP_FORMAT=$FP_FP32

# --- Operation Type ---
OP_SUM=0x05
OP_AVERAGE=0x06
export UDP_MOD_OPERATION=$OP_SUM   # SUM + software /8 (proven working)

# --- Simulation Config ---
export UDP_MOD_DRY_RUN=0
export NUM_CHUNKS=1024

# --- Collective ID (starting value; Gloo auto-increments per AllReduce) ---
# Full run: 10 epochs × 98 batches × 2 CIDs (barrier + AR) = ~1960 CIDs
export UDP_MOD_COLLECTIVE_ID=0x2000

# --- Flow Control ---
export UDP_MOD_MAX_IN_FLIGHT=10
export UDP_MOD_SEND_DELAY_US=50
export UDP_MOD_ITERATION_TIMEOUT=60

# --- Debug ---
export UDP_MOD_LOG_PACKETS=0

# --- Cleanup ---
echo "Cleaning up previous processes..."
kill -9 $(pgrep -f "python.*train_cifar10") 2>/dev/null || true
kill -9 $(lsof -t -i:29500) 2>/dev/null || true
sleep 1

# --- Run ---
LOG_FILE="${1:-logs/train_cifar10_$(date +%y_%m_%d_%H%M)}"
EPOCHS="${2:-10}"
BATCH_SIZE="${3:-64}"

echo "Config: FP=${UDP_MOD_FP_FORMAT}, OP=${UDP_MOD_OPERATION}, EPOCHS=${EPOCHS}, BATCH=${BATCH_SIZE}"
echo "Log: ${LOG_FILE}"
echo ""

python3 train_cifar10_distributed.py \
    --epochs "$EPOCHS" \
    --batch-size "$BATCH_SIZE" \
    2>&1 | tee "$LOG_FILE"

echo ""
echo "Training complete. Log saved to: $LOG_FILE"
