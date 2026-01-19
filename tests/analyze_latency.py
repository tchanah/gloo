#!/usr/bin/env python3
"""
Analyze per-chunk network latency from UDPmod allreduce pcap files.

This script computes the ACTUAL network latency by measuring the time difference
between sending a specific chunk and receiving its response (per-chunk round-trip).

Usage:
    python analyze_latency.py <pcap_file> [options]

Options:
    --node <N>          Source node to track (default: 0, based on port 10000+N)
    --chunk <N>         Specific chunk to analyze (default: analyze sample of chunks)
    --iter <N>          Iteration number to analyze (0-indexed, default: 0)
    --all-chunks        Analyze all chunks (warning: slow for 1024 chunks)
    --verbose           Print detailed packet info
"""

import subprocess
import argparse
import struct
import sys
from collections import defaultdict


# UDPmod packet header structure (16 bytes)
HEADER_SIZE = 16
BASE_PORT = 10000
SWITCH_PORT = 5684


def parse_header(hex_data):
    """Parse UDPmod header from hex string."""
    if len(hex_data) < HEADER_SIZE * 2:
        return None
    
    try:
        raw = bytes.fromhex(hex_data[:HEADER_SIZE * 2])
        
        collective_id = struct.unpack('<H', raw[0:2])[0]
        collective_type = raw[2]
        operation = raw[3]
        max_level = raw[6]
        current_level = raw[7]
        chunk_index = struct.unpack('<I', raw[8:12])[0]
        total_chunks = struct.unpack('<I', raw[12:16])[0]
        
        return {
            'collective_id': collective_id,
            'collective_type': collective_type,
            'operation': operation,
            'max_level': max_level,
            'current_level': current_level,
            'chunk_index': chunk_index,
            'total_chunks': total_chunks
        }
    except Exception:
        return None


def extract_packets(pcap_file, verbose=False):
    """Extract packet info using tshark."""
    cmd = [
        'tshark', '-r', pcap_file,
        '-T', 'fields',
        '-e', 'frame.time_epoch',
        '-e', 'udp.srcport',
        '-e', 'udp.dstport',
        '-e', 'udp.payload',
        '-E', 'separator=|'
    ]
    
    print(f"Extracting packets from {pcap_file}...")
    print("This may take a while for large files...")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"Error running tshark: {result.stderr}", file=sys.stderr)
        return []
    
    packets = []
    lines = result.stdout.strip().split('\n')
    total_lines = len(lines)
    
    for i, line in enumerate(lines):
        if not line.strip():
            continue
            
        if (i + 1) % 100000 == 0:
            print(f"  Processed {i + 1}/{total_lines} packets...")
            
        parts = line.split('|')
        if len(parts) < 4:
            continue
            
        try:
            timestamp = float(parts[0])
            src_port = int(parts[1])
            dst_port = int(parts[2])
            payload_hex = parts[3].replace(':', '')
            
            header = parse_header(payload_hex)
            if header is None:
                continue
            
            # Determine direction and node
            if BASE_PORT <= src_port < BASE_PORT + 8:
                direction = 'request'
                node = src_port - BASE_PORT
            elif dst_port >= BASE_PORT and dst_port < BASE_PORT + 8:
                direction = 'response'
                node = dst_port - BASE_PORT
            else:
                continue
                
            packets.append({
                'timestamp': timestamp,
                'src_port': src_port,
                'dst_port': dst_port,
                'direction': direction,
                'node': node,
                **header
            })
            
        except (ValueError, IndexError):
            continue
    
    print(f"  Extracted {len(packets)} valid packets")
    return packets


def group_by_iteration(packets):
    """Group packets by collective_id (iteration)."""
    iterations = defaultdict(list)
    for pkt in packets:
        iterations[pkt['collective_id']].append(pkt)
    return iterations


def compute_per_chunk_latency(packets, node=0, iteration=0, chunk=None, all_chunks=False, verbose=False):
    """Compute per-chunk round-trip latency."""
    
    # Group by iteration
    iterations = group_by_iteration(packets)
    
    # Sort available collective_ids and pick the nth one for the requested iteration
    sorted_coll_ids = sorted(iterations.keys())
    if iteration >= len(sorted_coll_ids):
        print(f"Error: Iteration {iteration} not found (only {len(sorted_coll_ids)} iterations available)")
        print(f"Available collective_ids: {[hex(k) for k in sorted_coll_ids[:10]]}...")
        return []
    
    coll_id = sorted_coll_ids[iteration]
    print(f"Analyzing iteration {iteration} (collective_id=0x{coll_id:04x})")
    
    iter_pkts = iterations[coll_id]
    
    # Determine total chunks and max_level from first packet
    if not iter_pkts:
        return []
    total_chunks = iter_pkts[0]['total_chunks']
    max_level = iter_pkts[0]['max_level']
    response_level = max_level + 1
    
    # Determine which chunks to analyze
    if chunk is not None:
        chunks_to_analyze = [chunk]
    elif all_chunks:
        chunks_to_analyze = list(range(total_chunks))
    else:
        # Sample: first, middle, and last chunks + a few others
        sample_chunks = [0, 1, 10, 100, 500, 1000, 1022, 1023]
        chunks_to_analyze = [c for c in sample_chunks if c < total_chunks]
    
    # Build lookup tables for fast matching
    # Key: (node, chunk_index), Value: list of (timestamp, packet)
    requests = defaultdict(list)
    responses = defaultdict(list)
    
    for pkt in iter_pkts:
        if pkt['node'] == node:
            key = pkt['chunk_index']
            if pkt['direction'] == 'request' and pkt['current_level'] == 0:
                requests[key].append(pkt)
            elif pkt['direction'] == 'response' and pkt['current_level'] == response_level:
                responses[key].append(pkt)
    
    results = []
    
    for chunk_idx in chunks_to_analyze:
        req_pkts = requests.get(chunk_idx, [])
        resp_pkts = responses.get(chunk_idx, [])
        
        if not req_pkts or not resp_pkts:
            if verbose:
                print(f"  Chunk {chunk_idx}: Missing {'request' if not req_pkts else 'response'} packet")
            continue
        
        # Take the first request and first response for this chunk
        req = req_pkts[0]
        resp = resp_pkts[0]
        
        latency_us = (resp['timestamp'] - req['timestamp']) * 1_000_000  # microseconds
        latency_ms = latency_us / 1000
        
        results.append({
            'chunk_index': chunk_idx,
            'request_time': req['timestamp'],
            'response_time': resp['timestamp'],
            'latency_us': latency_us,
            'latency_ms': latency_ms
        })
        
        if verbose:
            print(f"  Chunk {chunk_idx:4d}: Request {req['timestamp']:.6f} -> Response {resp['timestamp']:.6f} = {latency_us:.3f} µs ({latency_ms:.3f} ms)")
    
    return results


def print_statistics(results, iteration, node):
    """Print summary statistics."""
    if not results:
        print("\nNo valid latency measurements found.")
        return
    
    latencies_us = [r['latency_us'] for r in results]
    latencies_ms = [r['latency_ms'] for r in results]
    
    print("\n" + "=" * 70)
    print("PER-CHUNK NETWORK LATENCY STATISTICS")
    print("=" * 70)
    print(f"Iteration:         {iteration}")
    print(f"Node:              {node}")
    print(f"Chunks Analyzed:   {len(results)}")
    print("-" * 70)
    print(f"Average Latency:   {sum(latencies_us) / len(latencies_us):,.3f} µs ({sum(latencies_ms) / len(latencies_ms):.3f} ms)")
    print(f"Min Latency:       {min(latencies_us):,.3f} µs ({min(latencies_ms):.3f} ms)")
    print(f"Max Latency:       {max(latencies_us):,.3f} µs ({max(latencies_ms):.3f} ms)")
    
    if len(latencies_us) > 1:
        mean = sum(latencies_us) / len(latencies_us)
        variance = sum((x - mean) ** 2 for x in latencies_us) / len(latencies_us)
        std_dev = variance ** 0.5
        print(f"Std Deviation:     {std_dev:,.3f} µs ({std_dev/1000:.3f} ms)")
    
    print("=" * 70)
    
    # Per-chunk breakdown (show first few and last few)
    print("\nPer-Chunk Latencies (sample):")
    print("-" * 50)
    
    if len(results) <= 20:
        for r in results:
            print(f"  Chunk {r['chunk_index']:4d}: {r['latency_us']:10,.3f} µs")
    else:
        for r in results[:10]:
            print(f"  Chunk {r['chunk_index']:4d}: {r['latency_us']:10,.3f} µs")
        print("  ...")
        for r in results[-5:]:
            print(f"  Chunk {r['chunk_index']:4d}: {r['latency_us']:10,.3f} µs")


def main():
    parser = argparse.ArgumentParser(description='Analyze per-chunk UDPmod network latency from pcap')
    parser.add_argument('pcap_file', help='Path to the pcap file')
    parser.add_argument('--node', type=int, default=0, 
                        help='Source node to track (0-7, default: 0)')
    parser.add_argument('--chunk', type=int, default=None,
                        help='Specific chunk index to analyze')
    parser.add_argument('--iter', type=int, default=0,
                        help='Iteration to analyze (0-indexed, default: 0)')
    parser.add_argument('--all-chunks', action='store_true',
                        help='Analyze all chunks (slow)')
    parser.add_argument('--verbose', action='store_true',
                        help='Print detailed packet info')
    
    args = parser.parse_args()
    
    # Extract packets
    packets = extract_packets(args.pcap_file, args.verbose)
    
    if not packets:
        print("No packets found in pcap file")
        return 1
    
    # Compute per-chunk latencies
    results = compute_per_chunk_latency(
        packets, 
        node=args.node,
        iteration=args.iter,
        chunk=args.chunk,
        all_chunks=args.all_chunks,
        verbose=args.verbose
    )
    
    # Print results
    print_statistics(results, args.iter, args.node)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
