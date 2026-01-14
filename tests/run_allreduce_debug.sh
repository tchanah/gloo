# 1. Export Configuration Results
echo "Exporting environment variables..."

# Simulation Config
export UDP_MOD_DRY_RUN=0  # Need to receive responses from hardware
export NUM_CHUNKS=1024

# Debug Logging (Capture Headers)
export UDP_MOD_LOG_PACKETS=0

export UDP_MOD_COLLECTIVE_ID=0x1  # Non-zero to trigger fresh accelerator reset

# 2. Cleanup Previous Runs
echo "Cleaning up previous processes..."
kill -9 $(pgrep -f "python.*test_allreduce_8node.py") 2>/dev/null || true
# Kill process holding port 29500 (Master)
kill -9 $(lsof -t -i:29500) 2>/dev/null || true
sleep 1

# 3. Run the Test
# Use provided argument as log name, or default  
LOG_FILE="${1:-logs/log_single_iteration}"

echo "Running test... Output redirected to $LOG_FILE"
python3 test_allreduce_8node.py > "$LOG_FILE" 2>&1
echo "Test finished. Check $LOG_FILE for details."
