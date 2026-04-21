#!/bin/bash
# Verification Script for Optimal AllReduce Parameters
# Runs 10 iterations of the "sweet spot" configuration for each FP format.
#
# Usage: ./sweep_verification.sh [STARTING_COLLECTIVE_ID]
#   STARTING_COLLECTIVE_ID: hex value (default 0x1000). ID increments each test
#                           so hardware never sees a lower ID than a previous run.

# Create timestamped output directory
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M")
OUTPUT_DIR="logs/${TIMESTAMP}_verification"
mkdir -p "$OUTPUT_DIR"

RESULTS_FILE="${OUTPUT_DIR}/verification_results.csv"

# Cleanup function for Ctrl+C
cleanup() {
    echo ""
    echo "Interrupted! Cleaning up..."
    pkill -9 -f "python.*test_allreduce" 2>/dev/null
    kill -9 $(lsof -t -i:29500) 2>/dev/null
    exit 1
}
trap cleanup SIGINT SIGTERM

# Initialize results file
echo "fp_format,delay_us,max_in_flight,iteration,status,avg_time_ms,min_time_ms,max_time_ms,total_chunks" > "$RESULTS_FILE"

# Fixed config
export UDP_MOD_OPERATION=0x06  # SUM
export UDP_MOD_DRY_RUN=0
export NUM_CHUNKS=1024
export UDP_MOD_LOG_PACKETS=0
export UDP_MOD_ITERATION_TIMEOUT=60  # Faster STUCK detection for verification
export UDP_MOD_ITERATIONS=8        # Passed to Python test via env var

# Monotonically increasing collective ID
# Hardware drops packets with collective_id < current, so we must always go up.
COLLECTIVE_ID_START=${1:-0x1000}
COLLECTIVE_ID=$((COLLECTIVE_ID_START))

run_test() {
    local fp_format=$1
    local fp_name=$2
    local delay=$3
    local inflight=$4
    local iter=$5 # Iteration number
    
    # Set collective ID for this test (monotonically increasing)
    export UDP_MOD_COLLECTIVE_ID=$(printf "0x%04X" $COLLECTIVE_ID)
    
    LOG_FILE="${OUTPUT_DIR}/verify_${fp_name}_iter${iter}.log"
    
    echo "  [Run $iter/10] ${fp_name}: DELAY=${delay}us, IN_FLIGHT=${inflight} CID=${UDP_MOD_COLLECTIVE_ID}..."
    
    export UDP_MOD_FP_FORMAT=$fp_format
    export UDP_MOD_SEND_DELAY_US=$delay
    export UDP_MOD_MAX_IN_FLIGHT=$inflight
    
    # Cleanup any stuck processes
    pkill -9 -f "python.*test_allreduce" 2>/dev/null || true
    kill -9 $(lsof -t -i:29500) 2>/dev/null || true
    sleep 1
    
    # Run the test
    python3 test_allreduce_fp_formats.py > "$LOG_FILE" 2>&1
    exit_code=$?
    
    # Advance collective ID past all IDs this test consumed
    COLLECTIVE_ID=$(( COLLECTIVE_ID + UDP_MOD_ITERATIONS ))
    
    if [ $exit_code -ne 0 ]; then
        echo "    → FAILED/STUCK (exit code $exit_code)"
        echo "$fp_name,$delay,$inflight,$iter,STUCK,,,," >> "$RESULTS_FILE"
        pkill -9 -f "python.*test_allreduce" 2>/dev/null || true
        kill -9 $(lsof -t -i:29500) 2>/dev/null || true
        sleep 2
    else
        avg=$(grep "Average Time:" "$LOG_FILE" | awk '{print $3}')
        min=$(grep "Min Time:" "$LOG_FILE" | awk '{print $3}')
        max=$(grep "Max Time:" "$LOG_FILE" | awk '{print $3}')
        
        echo "    → SUCCESS: Avg=${avg}ms"
        echo "$fp_name,$delay,$inflight,$iter,SUCCESS,$avg,$min,$max,$NUM_CHUNKS" >> "$RESULTS_FILE"
    fi
}

echo "Starting verification sweep (10 runs per config)..."
echo "Output directory: ${OUTPUT_DIR}"
echo ""

# ============================================================
# 1. FP32: Delay 0us, InFlight 10
# ============================================================
echo "=== Verifying FP32 (0us / 10) ==="
for i in {1..10}; do
    run_test 0x00 "FP32" 0 10 $i
done
echo ""

# ============================================================
# 2. BF16: Delay 0us, InFlight 10
# ============================================================
echo "=== Verifying BF16 (0us / 10) ==="
for i in {1..10}; do
    run_test 0x01 "BF16" 0 10 $i
done
echo ""

# ============================================================
# 3. DLFloat: Delay 0us, InFlight 10
# ============================================================
echo "=== Verifying DLFloat (0us / 10) ==="
for i in {1..10}; do
    run_test 0x02 "DLFloat" 0 10 $i
done
echo ""

echo "=========================================="
echo "Verification complete! Results saved to: $RESULTS_FILE"
echo "=========================================="
