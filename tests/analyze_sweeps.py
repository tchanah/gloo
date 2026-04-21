import pandas as pd
import glob
import os
import sys

# List of specific files provided by the user
files = [
    "/u/fpga/chettige/pytorch/third_party/gloo/tests/logs/2026-02-04_16-35/sweep_results.csv",
    "/u/fpga/chettige/pytorch/third_party/gloo/tests/logs/2026-02-05_11-47/sweep_results.csv",
    "/u/fpga/chettige/pytorch/third_party/gloo/tests/logs/2026-02-05_22-05/sweep_results.csv",
    "/u/fpga/chettige/pytorch/third_party/gloo/tests/logs/2026-02-06_10-54/sweep_results.csv",
    "/u/fpga/chettige/pytorch/third_party/gloo/tests/logs/2026-02-06_13-37/sweep_results.csv",
    "/u/fpga/chettige/pytorch/third_party/gloo/tests/logs/2026-02-06_18-28/sweep_results.csv",
    "/u/fpga/chettige/pytorch/third_party/gloo/tests/logs/2026-02-07_10-14/sweep_results.csv"
]

dfs = []
for f in files:
    if os.path.exists(f):
        try:
            df = pd.read_csv(f)
            df['source_file'] = f
            dfs.append(df)
        except Exception as e:
            print(f"Error reading {f}: {e}")
    else:
        print(f"File not found: {f}")

if not dfs:
    print("No data found.")
    sys.exit(1)

all_data = pd.concat(dfs, ignore_index=True)

# Normalize column names (strip whitespace)
all_data.columns = [c.strip() for c in all_data.columns]
all_data['fp_format'] = all_data['fp_format'].astype(str).str.strip()
all_data['status'] = all_data['status'].astype(str).str.strip()

# Aggregate data
# Group by format, delay, max_in_flight
grouped = all_data.groupby(['fp_format', 'delay_us', 'max_in_flight']).agg({
    'status': lambda x: (x == 'SUCCESS').sum(),
    'avg_time_ms': 'mean',
    'source_file': 'count' # Total attempts
}).rename(columns={'status': 'success_count', 'source_file': 'total_attempts'})

grouped['success_rate'] = grouped['success_count'] / grouped['total_attempts']

# Filter for successful configurations
successful_configs = grouped[grouped['success_count'] > 0].copy()
successful_configs = successful_configs.reset_index()

# Sort by format and avg_time_ms
successful_configs = successful_configs.sort_values(by=['fp_format', 'avg_time_ms'])

# Print Top Results per Format
formats = successful_configs['fp_format'].unique()

print("\n=== TOP CONFIGURATIONS PER FORMAT ===")
for fmt in formats:
    print(f"\nFormat: {fmt}")
    subset = successful_configs[successful_configs['fp_format'] == fmt].head(10)
    print(subset[['delay_us', 'max_in_flight', 'avg_time_ms', 'success_rate', 'total_attempts']].to_string(index=False))

# Analysis of Range/Sweet Spot
print("\n=== DETAILED ANALYSIS ===")
for fmt in formats:
    print(f"\n--- {fmt} Landscape ---")
    data = successful_configs[successful_configs['fp_format'] == fmt]
    
    # Pivot to create a grid view for console
    try:
        pivot = data.pivot(index='delay_us', columns='max_in_flight', values='avg_time_ms')
        print("Average Time (ms) Grid [Rows=Delay, Cols=InFlight]:")
        pd.set_option('display.max_columns', None)
        pd.set_option('display.width', 1000)
        print(pivot)
    except Exception as e:
        print(f"Could not create pivot: {e}")

