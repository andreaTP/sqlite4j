#!/usr/bin/env python3
"""
Correlate WAT line counts with Java bytecode sizes for all WASM functions.

Usage:
  python3 correlate_wat_bytecode.py [wat_file] [bytecodes_json]

Outputs statistics and a data file for analysis.
"""

import json
import re
import sys
import statistics


def count_wat_lines_per_func(wat_path):
    """Parse WAT and return {func_index: line_count} for all functions."""
    funcs = {}
    current_func = None
    func_start = 0
    depth = 0

    with open(wat_path, 'r') as f:
        for line_no, line in enumerate(f, 1):
            stripped = line.strip()

            # Detect function start: (func (;NNN;) or (func $name
            if current_func is None:
                m = re.match(r'\(func\s+\(;(\d+);\)', stripped)
                if m:
                    current_func = int(m.group(1))
                    func_start = line_no
                    depth = 1
                    continue

            # Track nesting depth
            # Count opening parens that start blocks
            depth += stripped.count('(block') + stripped.count('(loop') + stripped.count('(if')
            # For general depth tracking, count top-level ( and )
            # Actually, simpler: just track when we see the closing ) at depth 0
            # We'll use a different approach: find the next (func or end of file

        # Simpler approach: find func boundaries by regex
    funcs = {}
    with open(wat_path, 'r') as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        m = re.match(r'\s*\(func\s+\(;(\d+);\)', lines[i])
        if m:
            func_idx = int(m.group(1))
            func_start = i
            # Find the end of this function by tracking paren depth
            depth = 0
            for j in range(i, len(lines)):
                line = lines[j]
                for ch in line:
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                if depth == 0:
                    func_end = j
                    line_count = func_end - func_start + 1
                    funcs[func_idx] = line_count
                    i = j + 1
                    break
            else:
                # Didn't find end
                i += 1
        else:
            i += 1

    return funcs


def main():
    wat_path = sys.argv[1] if len(sys.argv) > 1 else 'wasm-lib/libsqlite3_orig.wat'
    bytecodes_path = sys.argv[2] if len(sys.argv) > 2 else '/tmp/bytecodes_orig.json'

    print(f"Parsing WAT: {wat_path}")
    wat_lines = count_wat_lines_per_func(wat_path)
    print(f"  Found {len(wat_lines)} functions in WAT")

    print(f"Loading bytecodes: {bytecodes_path}")
    with open(bytecodes_path) as f:
        bytecodes = json.load(f)
    print(f"  Found {len(bytecodes)} methods in class file")

    # Match func_N from bytecodes with N from WAT
    pairs = []
    for method_name, bytecode_size in bytecodes.items():
        m = re.match(r'func_(\d+)', method_name)
        if m:
            func_idx = int(m.group(1))
            if func_idx in wat_lines:
                pairs.append((func_idx, wat_lines[func_idx], bytecode_size))

    print(f"\n  Matched {len(pairs)} functions with both WAT and bytecode data")

    # Sort by bytecode size descending
    pairs.sort(key=lambda x: -x[2])

    # Compute ratios
    ratios = []
    for func_idx, wat_count, bytecode_size in pairs:
        if wat_count > 0:
            ratio = bytecode_size / wat_count
            ratios.append((func_idx, wat_count, bytecode_size, ratio))

    # Statistics
    all_ratios = [r[3] for r in ratios]
    print(f"\n=== Bytecode/WAT-line ratio statistics ===")
    print(f"  Mean:   {statistics.mean(all_ratios):.2f} bytes/WAT-line")
    print(f"  Median: {statistics.median(all_ratios):.2f} bytes/WAT-line")
    print(f"  StdDev: {statistics.stdev(all_ratios):.2f}")
    print(f"  Min:    {min(all_ratios):.2f}")
    print(f"  Max:    {max(all_ratios):.2f}")

    # Percentiles
    sorted_ratios = sorted(all_ratios)
    n = len(sorted_ratios)
    for pct in [10, 25, 50, 75, 90, 95, 99]:
        idx = int(n * pct / 100)
        print(f"  P{pct:2d}:    {sorted_ratios[idx]:.2f}")

    # Show top 30 largest functions
    print(f"\n=== Top 30 functions by bytecode size ===")
    print(f"{'Func':>10} {'WAT lines':>10} {'Bytecode':>10} {'Ratio':>8} {'Predicted 8KB WAT':>18}")
    print('-' * 60)
    for func_idx, wat_count, bytecode_size, ratio in ratios[:30]:
        over = ' OVER' if bytecode_size > 8000 else ''
        print(f"  func_{func_idx:<5} {wat_count:>8} {bytecode_size:>8} B {ratio:>7.2f} {over}")

    # Key question: what WAT line threshold catches all >8KB functions?
    over_8k = [(idx, wat, bc, r) for idx, wat, bc, r in ratios if bc > 8000]
    if over_8k:
        min_wat_for_over_8k = min(w for _, w, _, _ in over_8k)
        max_wat_for_over_8k = max(w for _, w, _, _ in over_8k)
        print(f"\n=== Functions >8KB bytecode ===")
        print(f"  Count: {len(over_8k)}")
        print(f"  WAT lines range: {min_wat_for_over_8k} - {max_wat_for_over_8k}")
        print(f"  Ratios: {min(r for _, _, _, r in over_8k):.2f} - {max(r for _, _, _, r in over_8k):.2f}")

    # Find a safe WAT threshold
    # Use P10 ratio to find conservative WAT threshold (low ratio = more WAT lines per byte)
    p10_ratio = sorted_ratios[int(n * 0.10)]
    safe_wat_threshold = int(8000 / p10_ratio)
    print(f"\n=== Safe WAT-line threshold for 8KB bytecode ===")
    print(f"  Using P10 ratio ({p10_ratio:.2f}): split if WAT lines > {safe_wat_threshold}")

    # Check: does this threshold catch all >8KB functions?
    missed = [(idx, wat, bc) for idx, wat, bc, _ in ratios if bc > 8000 and wat <= safe_wat_threshold]
    if missed:
        print(f"  WARNING: {len(missed)} functions >8KB would be MISSED:")
        for idx, wat, bc in missed:
            print(f"    func_{idx}: {wat} WAT lines, {bc} bytes")
    else:
        print(f"  All {len(over_8k)} functions >8KB are caught by this threshold")

    # Also check: how many false positives?
    false_positives = [(idx, wat, bc) for idx, wat, bc, _ in ratios if bc <= 8000 and wat > safe_wat_threshold]
    print(f"  False positives (>threshold but <8KB): {len(false_positives)}")

    # Try different thresholds
    print(f"\n=== Threshold analysis ===")
    print(f"{'WAT threshold':>15} {'Catches >8KB':>15} {'False positives':>18} {'Total candidates':>18}")
    for thresh in [500, 750, 1000, 1250, 1500, 1750, 2000, 2500, 3000]:
        caught = sum(1 for _, wat, bc, _ in ratios if bc > 8000 and wat > thresh)
        total_over = len(over_8k)
        fp = sum(1 for _, wat, bc, _ in ratios if bc <= 8000 and wat > thresh)
        total = caught + fp
        print(f"  {thresh:>12} {caught:>8}/{total_over:<5} {fp:>13} {total:>13}")

    # Save detailed data for external analysis
    output_path = '/tmp/wat_bytecode_correlation.csv'
    with open(output_path, 'w') as f:
        f.write('func_index,wat_lines,bytecode_size,ratio\n')
        for func_idx, wat_count, bytecode_size, ratio in ratios:
            f.write(f'{func_idx},{wat_count},{bytecode_size},{ratio:.4f}\n')
    print(f"\nDetailed data saved to: {output_path}")


if __name__ == '__main__':
    main()
