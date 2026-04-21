# 1. Export Configuration Results
echo "Exporting environment variables..."

# FP Format Constants (for easy assignment)
FP_FP32=0x00     # 256 elements/chunk, 4 bytes each
FP_BF16=0x01     # 512 elements/chunk, 2 bytes each
FP_DLFLOAT=0x02  # 512 elements/chunk, 2 bytes each

# === SET TEST FORMAT HERE ===
export UDP_MOD_FP_FORMAT=$FP_FP32

# Operation Type Constants
OP_SUM=0x05
OP_AVERAGE=0x06

# === SET OPERATION TYPE HERE ===
export UDP_MOD_OPERATION=$OP_AVERAGE

# Simulation Config
export UDP_MOD_DRY_RUN=0  # Need to receive responses from hardware
export NUM_CHUNKS=1024

# Debug Logging (Capture Headers)
export UDP_MOD_LOG_PACKETS=0

export UDP_MOD_COLLECTIVE_ID=0x1111 # Non-zero to trigger fresh accelerator reset

# Flow control: max in-flight packets (default 8, tune for performance)
# Lower = more reliable, Higher = faster (if hardware can keep up)
export UDP_MOD_MAX_IN_FLIGHT=14

# Paced sending (recommended for optimum performance)
export UDP_MOD_SEND_DELAY_US=50

# 2. Cleanup Previous Runs
echo "Cleaning up previous processes..."
kill -9 $(pgrep -f "python.*test_allreduce") 2>/dev/null || true
# Kill process holding port 29500 (Master)
kill -9 $(lsof -t -i:29500) 2>/dev/null || true
sleep 1

# 3. Run the Test
# Use provided argument as log name, or default  
LOG_FILE="${1:-logs/log_single_iteration}"

echo "Running test with FP_FORMAT=${UDP_MOD_FP_FORMAT}... Output redirected to $LOG_FILE"
python3 test_allreduce_fp_formats.py > "$LOG_FILE" 2>&1
echo "Test finished. Check $LOG_FILE for details."

