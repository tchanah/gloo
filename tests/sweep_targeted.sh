#!/bin/bash
# Parameter sweep script - TARGETED SWEEP
# Usage: ./sweep_targeted.sh [STARTING_COLLECTIVE_ID]
#   STARTING_COLLECTIVE_ID: hex value (default 0x1000). ID increments each test.

# Create timestamped output directory
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M")
OUTPUT_DIR="logs/${TIMESTAMP}"
mkdir -p "$OUTPUT_DIR"

RESULTS_FILE="${OUTPUT_DIR}/sweep_results.csv"

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
echo "fp_format,delay_us,max_in_flight,status,avg_time_ms,min_time_ms,max_time_ms,iterations" > "$RESULTS_FILE"

# Fixed config
export UDP_MOD_OPERATION=0x06  # AVERAGE
export UDP_MOD_DRY_RUN=0
export NUM_CHUNKS=1024
export UDP_MOD_LOG_PACKETS=0
export UDP_MOD_ITERATION_TIMEOUT=60  # Lower timeout for sweep (default 120s is too slow)
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
    
    ((current_test++))
    
    # Set collective ID for this test (monotonically increasing)
    export UDP_MOD_COLLECTIVE_ID=$(printf "0x%04X" $COLLECTIVE_ID)
    
    LOG_FILE="${OUTPUT_DIR}/sweep_${fp_name}_d${delay}_f${inflight}.log"
    
    echo "[$current_test/$total_tests] ${fp_name}: DELAY=${delay}us, IN_FLIGHT=${inflight} CID=${UDP_MOD_COLLECTIVE_ID}..."
    
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
        echo "  → FAILED/STUCK (exit code $exit_code)"
        echo "$fp_name,$delay,$inflight,STUCK,,,," >> "$RESULTS_FILE"
        pkill -9 -f "python.*test_allreduce" 2>/dev/null || true
        kill -9 $(lsof -t -i:29500) 2>/dev/null || true
        sleep 2
    else
        avg=$(grep "Average Time:" "$LOG_FILE" | awk '{print $3}')
        min=$(grep "Min Time:" "$LOG_FILE" | awk '{print $3}')
        max=$(grep "Max Time:" "$LOG_FILE" | awk '{print $3}')
        iters=$(grep "Total Iterations:" "$LOG_FILE" | awk '{print $3}')
        
        echo "  → SUCCESS: Avg=${avg}ms, Min=${min}ms, Max=${max}ms"
        echo "$fp_name,$delay,$inflight,SUCCESS,$avg,$min,$max,$iters" >> "$RESULTS_FILE"
    fi
}




# ============================================================
# FULL GRID SWEEP: 3 formats x 6 inflights x 6 delays = 108 tests
# ============================================================
DELAYS=(0 25 50)
INFLIGHTS=(10 12 14 16 24)

total_tests=$(( 3 * ${#DELAYS[@]} * ${#INFLIGHTS[@]} ))
current_test=0

echo "Starting full grid sweep: ${total_tests} tests"
echo "Output directory: ${OUTPUT_DIR}"
echo ""

# ============ FP32 ============
echo "=== Testing FP32 ==="
for delay in "${DELAYS[@]}"; do
    for inflight in "${INFLIGHTS[@]}"; do
        run_test 0x00 "FP32" $delay $inflight
    done
done

# ============ BF16 ============
echo ""
echo "=== Testing BF16 ==="
for delay in "${DELAYS[@]}"; do
    for inflight in "${INFLIGHTS[@]}"; do
        run_test 0x01 "BF16" $delay $inflight
    done
done

# ============ DLFloat ============
echo ""
echo "=== Testing DLFloat ==="
for delay in "${DELAYS[@]}"; do
    for inflight in "${INFLIGHTS[@]}"; do
        run_test 0x02 "DLFloat" $delay $inflight
    done
done

echo ""
echo "=========================================="
echo "Sweep complete! Results saved to: $RESULTS_FILE"
echo "=========================================="
echo ""
echo "Best configurations per format (sorted by average time):"
echo ""
for fp_name in "FP32" "BF16" "DLFloat"; do
    echo "=== $fp_name ==="
    grep "^$fp_name.*SUCCESS" "$RESULTS_FILE" | sort -t',' -k5 -n | head -3
    echo ""
done

