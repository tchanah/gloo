#!/bin/bash
# ============================================================
# Runner: Persistent AllReduce Test (no process respawn)
# ============================================================
# Tests that collective_id auto-increment works by running
# multiple AllReduce iterations within a single process lifetime.
#
# Usage: ./run_persistent_test.sh [STARTING_COLLECTIVE_ID]
# ============================================================

cd "$(dirname "$0")"
mkdir -p logs

echo "=== Persistent AllReduce Test ==="

# --- FP Format ---
export UDP_MOD_FP_FORMAT=0x00   # FP32

# --- Operation ---
export UDP_MOD_OPERATION=0x06   # AVERAGE (verified 8/8 in sweep)

# --- Simulation Config ---
export UDP_MOD_DRY_RUN=0
export NUM_CHUNKS=1024             # Small (4 chunks = 4KB) for quick test

# --- Collective ID ---
export UDP_MOD_COLLECTIVE_ID=${1:-0x2000}

# --- Flow Control ---
export UDP_MOD_MAX_IN_FLIGHT=10
export UDP_MOD_SEND_DELAY_US=50
export UDP_MOD_ITERATION_TIMEOUT=30

# --- Iterations ---
export UDP_MOD_ITERATIONS=8

# --- Debug ---
export UDP_MOD_LOG_PACKETS=0

# --- Cleanup ---
echo "Cleaning up previous processes..."
kill -9 $(pgrep -f "python.*test_persistent") 2>/dev/null || true
kill -9 $(pgrep -f "python.*test_allreduce") 2>/dev/null || true
kill -9 $(lsof -t -i:29500) 2>/dev/null || true
sleep 1

# --- Run ---
LOG_FILE="logs/persistent_test_$(date +%y_%m_%d_%H%M).log"

echo "Config: FP=${UDP_MOD_FP_FORMAT}, OP=${UDP_MOD_OPERATION}, CID=${UDP_MOD_COLLECTIVE_ID}"
echo "Chunks: ${NUM_CHUNKS}, Iterations: ${UDP_MOD_ITERATIONS}"
echo "Log: ${LOG_FILE}"
echo ""

python3 test_persistent_allreduce.py 2>&1 | tee "$LOG_FILE"

echo ""
echo "Test complete. Log saved to: $LOG_FILE"
