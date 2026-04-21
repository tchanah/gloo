#!/bin/bash
# Parameter sweep script with timeout handling
# Usage: ./sweep_parameters.sh
# Tests all combinations of FP formats, delays, and in-flight counts

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
export UDP_MOD_OPERATION=0x05  # SUM
export UDP_MOD_DRY_RUN=0
export NUM_CHUNKS=1024
export UDP_MOD_LOG_PACKETS=0
export UDP_MOD_COLLECTIVE_ID=0x1111

# Parameter ranges to sweep
FP_FORMATS=(0x00 0x01 0x02)  # FP32, BF16, DLFloat
FP_NAMES=("FP32" "BF16" "DLFloat")
DELAYS=(25 50 75 100)
INFLIGHTS=(10 12 14 16 18)

total_tests=$((${#FP_FORMATS[@]} * ${#DELAYS[@]} * ${#INFLIGHTS[@]}))
current_test=0

for fp_idx in "${!FP_FORMATS[@]}"; do
    fp_format="${FP_FORMATS[$fp_idx]}"
    fp_name="${FP_NAMES[$fp_idx]}"
    export UDP_MOD_FP_FORMAT=$fp_format
    
    for delay in "${DELAYS[@]}"; do
        for inflight in "${INFLIGHTS[@]}"; do
            ((current_test++))
            LOG_FILE="${OUTPUT_DIR}/sweep_${fp_name}_d${delay}_f${inflight}.log"
            
            echo "[$current_test/$total_tests] ${fp_name}: DELAY=${delay}us, IN_FLIGHT=${inflight}..."
            
            # Set the sweep parameters
            export UDP_MOD_SEND_DELAY_US=$delay
            export UDP_MOD_MAX_IN_FLIGHT=$inflight
            
            # Cleanup any stuck processes
            pkill -9 -f "python.*test_allreduce" 2>/dev/null || true
            kill -9 $(lsof -t -i:29500) 2>/dev/null || true
            sleep 1
            
            # Run the test (internal timeout handles stuck iterations)
            python3 test_allreduce_fp_formats.py > "$LOG_FILE" 2>&1
            exit_code=$?
            
            if [ $exit_code -ne 0 ]; then
                # Test failed (likely stuck iteration detected internally)
                echo "  → FAILED/STUCK (exit code $exit_code)"
                echo "$fp_name,$delay,$inflight,STUCK,,,," >> "$RESULTS_FILE"
                
                # Force kill any remaining processes
                pkill -9 -f "python.*test_allreduce" 2>/dev/null || true
                kill -9 $(lsof -t -i:29500) 2>/dev/null || true
                sleep 2
            elif [ $exit_code -ne 0 ]; then
                echo "  → FAILED (exit code $exit_code)"
                echo "$fp_name,$delay,$inflight,FAILED,,,," >> "$RESULTS_FILE"
            else
                # Success - extract stats from log
                avg=$(grep "Average Time:" "$LOG_FILE" | awk '{print $3}')
                min=$(grep "Min Time:" "$LOG_FILE" | awk '{print $3}')
                max=$(grep "Max Time:" "$LOG_FILE" | awk '{print $3}')
                iters=$(grep "Total Iterations:" "$LOG_FILE" | awk '{print $3}')
                
                echo "  → SUCCESS: Avg=${avg}ms, Min=${min}ms, Max=${max}ms"
                echo "$fp_name,$delay,$inflight,SUCCESS,$avg,$min,$max,$iters" >> "$RESULTS_FILE"
            fi
        done
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
