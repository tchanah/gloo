#!/bin/bash
# SW Latency Timing Measurement Script
#
# Measures PyTorch/Gloo software overhead (pre-send + post-recv) for 1KB (1 chunk)
# AllReduce across all 3 data formats. Timing comes from pair.cc instrumentation
# (TIMING_LOG lines on stderr).
#
# Usage: ./sw_latency_timing.sh [STARTING_COLLECTIVE_ID]
#   STARTING_COLLECTIVE_ID: hex value (default 0x3000). Advance past prior runs.
#
# Output per format (inside logs/<TIMESTAMP>/):
#   timing_FP32.csv      — TIMING_LOG CSV: rank,chunks,coll_id,pre_send_ns,post_recv_ns,sw_total_ns
#   timing_BF16.csv
#   timing_DLFloat.csv
#   allreduce_FP32.log   — full stdout + non-timing stderr
#   allreduce_BF16.log
#   allreduce_DLFloat.log
#   summary.txt          — mean/std/min/max SW overhead per format
#
# Requires: pair.cc instrumented with TIMING_LOG output (UDP_MOD_LOG_TIMING=1 set below).
# Rebuild Gloo before running if pair.cc was just modified.

# NOTE: No set -e here intentionally — python3 may return non-zero on STUCK
# and we want to handle that gracefully, not abort the whole script.

# ── Output directory (matches sweep_targeted.sh convention) ───────────────────
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M")
OUTPUT_DIR="logs/${TIMESTAMP}"
mkdir -p "$OUTPUT_DIR"
echo "Output directory: $OUTPUT_DIR"

# ── Cleanup on Ctrl+C ─────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "Interrupted! Cleaning up..."
    pkill -9 -f "python.*test_allreduce" 2>/dev/null || true
    local pids
    pids=$(lsof -t -i:29500 2>/dev/null) && [ -n "$pids" ] && kill -9 $pids || true
    exit 1
}
trap cleanup SIGINT SIGTERM

# ── Fixed config ──────────────────────────────────────────────────────────────
export UDP_MOD_DRY_RUN=0
export UDP_MOD_LOG_PACKETS=0
export UDP_MOD_LOG_TIMING=1           # Enable pair.cc SW timing instrumentation

export NUM_CHUNKS=1                   # 1 chunk = 1KB packet — pure latency measurement
export UDP_MOD_ITERATIONS=10240          # 10 runs to start; scale up once validated

export UDP_MOD_OPERATION=0x05         # SUM
export UDP_MOD_MAX_IN_FLIGHT=10      # Proven stable setting
export UDP_MOD_SEND_DELAY_US=50       # Proven stable setting
export UDP_MOD_ITERATION_TIMEOUT=30   # DLFloat needs >=30s

# ── Collective ID tracking ────────────────────────────────────────────────────
# Must be monotonically increasing across ALL runs on the same FPGA session.
# Pass a higher starting ID each time you re-run (no FPGA power cycle between runs).
COLLECTIVE_ID_START=${1:-0x3000}
COLLECTIVE_ID=$((COLLECTIVE_ID_START))

# ── Summary file ─────────────────────────────────────────────────────────────
SUMMARY_FILE="${OUTPUT_DIR}/summary.txt"
{
    echo "SW Overhead Timing Summary -- $(date)"
    echo "Config: NUM_CHUNKS=${NUM_CHUNKS}, ITERATIONS=${UDP_MOD_ITERATIONS}, DELAY=${UDP_MOD_SEND_DELAY_US}us, IN_FLIGHT=${UDP_MOD_MAX_IN_FLIGHT}"
    echo "CSV columns: rank,chunks,coll_id,pre_send_ns,post_recv_ns,sw_total_ns"
    echo ""
} > "$SUMMARY_FILE"

# ── Helper: kill stale processes safely ──────────────────────────────────────
kill_stale() {
    pkill -9 -f "python.*test_allreduce" 2>/dev/null || true
    local pids
    pids=$(lsof -t -i:29500 2>/dev/null) && [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
}

# ── run_format <fp_format_hex> <fp_name> ──────────────────────────────────────
run_format() {
    local fp_format=$1
    local fp_name=$2

    echo ""
    echo "======================================================"
    echo " Testing ${fp_name} (format=${fp_format})"
    echo " CID start: $(printf '0x%04X' $COLLECTIVE_ID)"
    echo " Chunks: ${NUM_CHUNKS} (1KB), Iterations: ${UDP_MOD_ITERATIONS}"
    echo "======================================================"

    export UDP_MOD_FP_FORMAT=$fp_format
    export UDP_MOD_COLLECTIVE_ID=$(printf "0x%04X" $COLLECTIVE_ID)

    local LOG_FILE="${OUTPUT_DIR}/allreduce_${fp_name}.log"
    local TIMING_CSV="${OUTPUT_DIR}/timing_${fp_name}.csv"
    local STDERR_TMP="${OUTPUT_DIR}/.stderr_${fp_name}.tmp"

    # CSV header for timing file
    echo "rank,chunks,coll_id,pre_send_ns,post_recv_ns,sw_total_ns" > "$TIMING_CSV"

    # Kill any stale processes from a prior run
    kill_stale
    sleep 1

    # Run test — capture exit code explicitly WITHOUT triggering set -e
    #   stdout -> allreduce log (iteration results from Python)
    #   stderr -> temp file   (TIMING_LOG lines from pair.cc + any Python warnings)
    local exit_code=0
    python3 test_allreduce_fp_formats.py > "$LOG_FILE" 2> "$STDERR_TMP" || exit_code=$?

    # Extract TIMING_LOG lines from stderr -> CSV (strip the "TIMING_LOG," prefix)
    grep "^TIMING_LOG," "$STDERR_TMP" | sed 's/^TIMING_LOG,//' >> "$TIMING_CSV" || true

    # Non-TIMING_LOG stderr lines (Python warnings, errors) -> allreduce log
    grep -v "^TIMING_LOG," "$STDERR_TMP" >> "$LOG_FILE" || true
    rm -f "$STDERR_TMP"

    # Advance collective ID past all IDs this run consumed (+10 safety margin)
    COLLECTIVE_ID=$(( COLLECTIVE_ID + UDP_MOD_ITERATIONS + 10 ))

    if [ $exit_code -ne 0 ]; then
        echo "  --> FAILED/STUCK (exit code $exit_code) -- check $LOG_FILE"
        echo "${fp_name}: FAILED (exit_code=${exit_code})" >> "$SUMMARY_FILE"
        kill_stale
        sleep 2
        return
    fi

    echo "  --> SUCCESS. Timing data: $TIMING_CSV"

    # Compute stats from the timing CSV (sw_total_ns = column 6)
    # awk handles the stats — no Python/numpy needed in the script
    local stats
    stats=$(awk -F',' '
        NR==1 { next }          # skip header
        $6+0 > 0 {              # skip zero/invalid rows
            sum += $6; sumsq += $6*$6; n++;
            if (first || $6 < mn) { mn=$6; first=0 }
            if ($6 > mx) mx=$6;
        }
        BEGIN { first=1; mn=0; mx=0 }
        END {
            if (n > 0) {
                mean = sum/n;
                std  = sqrt(sumsq/n - mean*mean);
                printf "N=%d  mean=%.0f ns  std=%.0f ns  min=%.0f ns  max=%.0f ns  (%.3f us +/- %.3f us)",
                       n, mean, std, mn, mx, mean/1000.0, std/1000.0
            } else {
                print "No timing data found in CSV"
            }
        }' "$TIMING_CSV")

    echo "  --> SW overhead: $stats"
    echo "${fp_name}: $stats" >> "$SUMMARY_FILE"
}

# ── MAIN ─────────────────────────────────────────────────────────────────────
echo "Starting SW overhead timing measurement"
echo "Output: $OUTPUT_DIR"

run_format 0x00 "FP32"
run_format 0x01 "BF16"
run_format 0x02 "DLFloat"

# ── Final summary ─────────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo " SW Latency Timing Complete"
echo "============================================"
echo ""
cat "$SUMMARY_FILE"
echo ""
echo "Timing CSVs:"
ls -1 "${OUTPUT_DIR}"/timing_*.csv
echo ""
echo "To scale up to 10240 runs, set UDP_MOD_ITERATIONS=10240 and re-run:"
echo "  UDP_MOD_ITERATIONS=10240 ./sw_latency_timing.sh $(printf '0x%04X' $COLLECTIVE_ID)"
