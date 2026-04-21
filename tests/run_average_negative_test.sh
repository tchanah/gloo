#!/bin/bash
# ============================================================
# Runner: Test AVERAGE with negative/mixed-sign values
# ============================================================
# Isolates whether AVERAGE has precision issues with certain
# value distributions (positive vs negative vs mixed).
# ============================================================

cd "$(dirname "$0")"
mkdir -p logs

echo "=== AVERAGE Negative Value Test ==="

# --- Config (same as sweep) ---
export UDP_MOD_FP_FORMAT=0x00
export UDP_MOD_OPERATION=0x06   # AVERAGE
export UDP_MOD_DRY_RUN=0
export NUM_CHUNKS=1024

# --- CID: must be past all previous runs ---
export UDP_MOD_COLLECTIVE_ID=${1:-0x4000}

# --- Flow Control ---
export UDP_MOD_MAX_IN_FLIGHT=10
export UDP_MOD_SEND_DELAY_US=50
export UDP_MOD_ITERATION_TIMEOUT=30

export UDP_MOD_LOG_PACKETS=0

# --- Cleanup ---
kill -9 $(pgrep -f "python.*test_average") 2>/dev/null || true
kill -9 $(pgrep -f "python.*test_persistent") 2>/dev/null || true
kill -9 $(lsof -t -i:29500) 2>/dev/null || true
sleep 1

LOG_FILE="logs/avg_negative_$(date +%y_%m_%d_%H%M).log"

echo "CID: ${UDP_MOD_COLLECTIVE_ID}"
echo "Log: ${LOG_FILE}"
echo ""

python3 test_average_negative.py 2>&1 | tee "$LOG_FILE"
