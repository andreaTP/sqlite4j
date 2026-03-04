#!/usr/bin/env python3
"""
Auto-detect splitting configuration for a WASM function from its WAT representation.

Usage:
  python3 auto_detect_config.py [wat_file] [func_index]
  python3 auto_detect_config.py --all [wat_file]        # detect all >2500 WAT lines
  python3 auto_detect_config.py --validate [wat_file]    # validate against known configs

Detects:
  - param_types, declared_types (from function signature)
  - frame_pointer_local, orig_frame_size (from stack frame setup pattern)
  - strategy (br_table vs block_extraction, based on dispatch pattern)
  - opcode_local, handler_container_label, num_structural, structural_types (br_table)
  - extraction_depth(s), min_block_size (block_extraction)
"""

import re
import sys
import json

from split_wasm import parse_block_structure


def find_func_boundaries(wat_lines, func_index):
    """Find start and end line indices for a function by its index."""
    pattern = re.compile(rf'\(func\s+\(;{func_index};\)')
    start = None
    for i, line in enumerate(wat_lines):
        if pattern.search(line):
            start = i
            break
    if start is None:
        return None, None

    # Find end by tracking paren depth
    depth = 0
    for j in range(start, len(wat_lines)):
        for ch in wat_lines[j]:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
        if depth == 0:
            return start, j
    return start, len(wat_lines) - 1


def parse_param_types(func_line):
    """Extract parameter types from function declaration line."""
    m = re.search(r'\(param\s+([^)]+)\)', func_line)
    if not m:
        return []
    return m.group(1).split()


def parse_result_type(func_line):
    """Extract result type from function declaration line."""
    m = re.search(r'\(result\s+([^)]+)\)', func_line)
    if not m:
        return None
    return m.group(1).strip()


def parse_declared_types(wat_lines, func_start):
    """Extract declared local types from the line(s) after the func declaration."""
    types = []
    for i in range(func_start + 1, min(func_start + 5, len(wat_lines))):
        line = wat_lines[i].strip()
        # Match (local i32 i32 i64 ...)
        m = re.match(r'\(local\s+([^)]+)\)', line)
        if m:
            types.extend(m.group(1).split())
            break
        # Stop if we hit an instruction (not a local declaration)
        if not line.startswith('(local'):
            break
    return types


def detect_frame_setup(wat_lines, func_start, func_end):
    """
    Detect the stack frame setup pattern:
      global.get 0
      i32.const N        <- orig_frame_size
      i32.sub
      local.tee K        <- frame_pointer_local
      global.set 0
    """
    for i in range(func_start + 1, min(func_start + 10, func_end)):
        line = wat_lines[i].strip()
        if line == 'global.get 0':
            # Check next lines for the pattern
            remaining = []
            for j in range(i + 1, min(i + 5, func_end)):
                remaining.append(wat_lines[j].strip())

            if len(remaining) >= 3:
                # i32.const N
                m_const = re.match(r'i32\.const\s+(\d+)', remaining[0])
                # i32.sub
                is_sub = remaining[1] == 'i32.sub'
                # local.tee K
                m_tee = re.match(r'local\.tee\s+(\d+)', remaining[2])

                if m_const and is_sub and m_tee:
                    return int(m_tee.group(1)), int(m_const.group(1))

    return None, None


def find_br_tables(wat_lines, func_start, func_end):
    """
    Find all br_table instructions and count their entries.
    Returns list of (line_index, entry_count, targets).
    """
    br_tables = []
    for i in range(func_start, func_end + 1):
        line = wat_lines[i].strip()
        if 'br_table' in line:
            # Count the entries: each entry is a number followed by (;@N;)
            entries = re.findall(r'(\d+)\s+\(;@(\d+);\)', line)
            if entries:
                targets = [(int(depth), int(label)) for depth, label in entries]
                br_tables.append((i, len(entries), targets))
    return br_tables


def compute_nesting_depth(wat_lines, func_start, target_line):
    """Compute the block nesting depth at a given line."""
    depth = 0
    for i in range(func_start, target_line):
        line = wat_lines[i].strip()
        # Opening constructs
        for kw in ['block', 'loop', 'if']:
            if re.match(rf'{kw}\b', line) or re.match(rf'\({kw}\b', line):
                # Check it's not inside a comment
                if f';; label = @' in line or line.startswith(kw) or line.startswith(f'({kw}'):
                    depth += 1
        # Closing
        if line == 'end' or line.startswith('end '):
            depth -= 1
    return depth


def detect_structural_labels(wat_lines, func_start, func_end, handler_container_depth):
    """
    Detect structural label types (block/loop/if) from the outermost to the
    handler container. Returns dict {label_number: type_string}.

    The handler container is the block wrapping all br_table target blocks.
    Structural labels are all the nesting levels OUTSIDE the handler container.
    """
    structural = {}
    current_label = 1  # Labels start at @1
    depth = 0

    for i in range(func_start + 1, func_end + 1):
        line = wat_lines[i].strip()

        # Check for block/loop/if with label annotation
        for kw in ['block', 'loop', 'if']:
            m = re.match(rf'({kw})\s*(?:\(result [^)]+\))?\s*;;\s*label\s*=\s*@(\d+)', line)
            if not m:
                m = re.match(rf'\({kw}\s*(?:\(result [^)]+\))?\s*;;\s*label\s*=\s*@(\d+)', line)
                if m:
                    label_num = int(m.group(1))
                    if label_num < handler_container_depth:
                        structural[label_num] = kw
                    return structural  # Probably wrong, let me try another approach
            if m:
                label_num = int(m.group(2)) if len(m.groups()) >= 2 else int(m.group(1))
                if label_num < handler_container_depth:
                    structural[label_num] = kw

        if depth >= handler_container_depth:
            break

    return structural


def detect_structural_labels_v2(wat_lines, func_start, func_end):
    """
    Detect the handler container and structural labels.

    Strategy:
    1. Find the staircase of consecutive block labels ending at @max_label
       (the innermost block, which contains the br_table).
    2. The handler container is identified as the first block in the staircase
       that is NOT a direct br_table target. If all are targets, the outermost
       block in the staircase is the container.

    Returns (structural_types_dict, handler_container_label).
    """
    # First, find the main br_table (the one with the most entries)
    br_tables = find_br_tables(wat_lines, func_start, func_end)
    if not br_tables:
        return {}, None

    main_bt = max(br_tables, key=lambda x: x[1])
    main_br_table_line = main_bt[0]
    targets = main_bt[2]
    target_labels = set(label for _, label in targets)
    max_label = max(target_labels)

    # Collect all labeled blocks/loops/ifs before the br_table
    label_pattern = re.compile(r'^(block|loop|if)\b.*?;;\s*label\s*=\s*@(\d+)')
    labels_in_order = []  # list of (line_index, label_num, type)

    for i in range(func_start + 1, main_br_table_line):
        line = wat_lines[i].strip()
        m = label_pattern.match(line)
        if m:
            labels_in_order.append((i, int(m.group(2)), m.group(1)))

    if not labels_in_order:
        return {}, None

    # Find the staircase ending at @max_label by scanning backwards.
    # The staircase is consecutive block labels on consecutive lines.
    max_label_idx = None
    for j in range(len(labels_in_order) - 1, -1, -1):
        if labels_in_order[j][1] == max_label:
            max_label_idx = j
            break

    if max_label_idx is None:
        return {}, None

    # Walk backwards from max_label to find the staircase start
    staircase_start = max_label_idx
    for j in range(max_label_idx - 1, -1, -1):
        prev_line, prev_label, prev_type = labels_in_order[j]
        curr_line, curr_label, curr_type = labels_in_order[j + 1]

        if (curr_label == prev_label + 1 and
                curr_type == 'block' and prev_type == 'block' and
                curr_line - prev_line <= 2):
            staircase_start = j
        else:
            break

    staircase_labels = [labels_in_order[j][1] for j in range(staircase_start, max_label_idx + 1)]

    # Determine the handler container.
    # Default: the outermost block in the staircase is the container.
    # Exception: if the label just BEFORE the staircase is a LOOP with a gap > 2
    # lines, the outermost staircase block is a structural wrapper, and the
    # container is one block deeper.
    handler_container_label = staircase_labels[0]
    if staircase_start > 0:
        prev_line, prev_label, prev_type = labels_in_order[staircase_start - 1]
        stair_line = labels_in_order[staircase_start][0]
        gap = stair_line - prev_line
        if prev_type == 'loop' and gap > 2 and len(staircase_labels) > 1:
            handler_container_label = staircase_labels[1]

    # Collect structural labels: all labels before the handler container
    structural_types = {}
    for _, label_num, label_type in labels_in_order:
        if label_num < handler_container_label:
            structural_types[label_num] = label_type

    return structural_types, handler_container_label


def detect_opcode_local(wat_lines, func_start, br_table_line):
    """
    Find the opcode local: the local.tee K instruction right before the br_table.
    Pattern: local.tee K followed by br_table.
    """
    # Search backwards from br_table line
    for i in range(br_table_line - 1, max(func_start, br_table_line - 10), -1):
        line = wat_lines[i].strip()
        m = re.match(r'local\.tee\s+(\d+)', line)
        if m:
            return int(m.group(1))
    return None


def detect_block_extraction_config(wat_lines, func_start, func_end):
    """Auto-detect extraction_depths and min_block_size for block_extraction strategy.

    Uses a "fan-out depth" heuristic: finds the shallowest depth where the function's
    block structure branches into multiple substantial blocks (>= min_block_size lines).
    If any block at that depth is too large for a helper (> max_helper_lines), also
    searches deeper depths for sub-extraction targets.

    Returns (extraction_depths, min_block_size).
    """
    func_lines = [wat_lines[i] for i in range(func_start, func_end + 1)]
    blocks = parse_block_structure(func_lines)

    min_block_size = 200
    max_helper_lines = 2500  # ~7.5KB bytecode at 3 bytes/WAT-line

    # Group blocks by depth, filter to those >= min_block_size
    blocks_by_depth = {}
    for idx, b in enumerate(blocks):
        if b['end_line'] is not None:
            size = b['end_line'] - b['start_line']
            if size >= min_block_size:
                blocks_by_depth.setdefault(b['depth'], []).append((idx, size))

    if not blocks_by_depth:
        return [1], min_block_size

    # Find primary fan-out depth: shallowest depth with >= 2 qualifying blocks
    extraction_depths = []
    for depth in sorted(blocks_by_depth.keys()):
        candidates = blocks_by_depth[depth]
        if len(candidates) >= 2:
            extraction_depths.append(depth)
            break

    # If no depth has >= 2 blocks, fall back to the depth with the single largest block
    if not extraction_depths:
        best_depth = max(blocks_by_depth.keys(),
                         key=lambda d: max(s for _, s in blocks_by_depth[d]))
        extraction_depths.append(best_depth)

    # Check if any target at primary depth is too large for a helper.
    # If so, add deeper depths to catch sub-blocks within oversized targets.
    primary_depth = extraction_depths[0]
    has_oversized = any(size > max_helper_lines
                        for _, size in blocks_by_depth.get(primary_depth, []))

    if has_oversized:
        for deeper in sorted(blocks_by_depth.keys()):
            if deeper > primary_depth and deeper not in extraction_depths:
                deeper_candidates = blocks_by_depth[deeper]
                if len(deeper_candidates) >= 2:
                    extraction_depths.append(deeper)
                    break

    return extraction_depths, min_block_size


def auto_detect_config(wat_lines, func_index):
    """
    Auto-detect a complete splitting config for a function.
    Returns a config dict or None if the function can't be found.
    """
    func_start, func_end = find_func_boundaries(wat_lines, func_index)
    if func_start is None:
        return None

    func_line = wat_lines[func_start]
    param_types = parse_param_types(func_line)
    result_type = parse_result_type(func_line)
    declared_types = parse_declared_types(wat_lines, func_start)

    frame_ptr_local, frame_size = detect_frame_setup(wat_lines, func_start, func_end)

    # Detect strategy
    br_tables = find_br_tables(wat_lines, func_start, func_end)

    # Large br_table (>10 entries) = br_table dispatch strategy
    large_bt = [bt for bt in br_tables if bt[1] > 10]

    config = {
        'func_index': func_index,
        'param_types': param_types,
        'declared_types': declared_types,
        'frame_pointer_local': frame_ptr_local,
        'orig_frame_size': frame_size,
        'wat_lines': func_end - func_start + 1,
    }

    if result_type:
        config['has_result'] = True
        config['result_type'] = result_type
    else:
        config['has_result'] = False

    if large_bt:
        config['strategy'] = 'br_table'
        # Use the largest br_table as the main dispatch
        main_bt = max(large_bt, key=lambda x: x[1])
        config['num_handlers'] = main_bt[1]

        # Detect opcode local
        opcode_local = detect_opcode_local(wat_lines, func_start, main_bt[0])
        config['opcode_local'] = opcode_local

        # Detect structural labels and handler container
        structural_types, handler_container = detect_structural_labels_v2(
            wat_lines, func_start, func_end
        )
        config['structural_types'] = structural_types
        config['handler_container_label'] = handler_container
        config['num_structural'] = handler_container  # structural labels are 1..handler_container-1, plus @handler_container

        # Estimate num_groups from bytecode size estimate.
        # Use 4KB target per group — conservative, because per-handler bytecode
        # varies 2-5x and overshooting group count (13% penalty at 1.5x) is much
        # cheaper than undershoot (correctness bugs with too-large groups).
        estimated_bytecode = (func_end - func_start + 1) * 3.0
        min_groups = max(2, int(estimated_bytecode / 4000) + 1)
        config['estimated_num_groups'] = min_groups
    else:
        config['strategy'] = 'block_extraction'
        depths, min_size = detect_block_extraction_config(
            wat_lines, func_start, func_end
        )
        if len(depths) == 1:
            config['extraction_depth'] = depths[0]
        else:
            config['extraction_depths'] = depths
        config['min_block_size'] = min_size

    return config


def format_config_as_python(config):
    """Format a config dict as Python code similar to split_wasm.py configs."""
    lines = []
    lines.append(f"FUNC_{config['func_index']}_CONFIG = {{")
    lines.append(f"    'func_index': {config['func_index']},")
    lines.append(f"    'strategy': '{config['strategy']}',")

    if config['strategy'] == 'br_table':
        lines.append(f"    'num_groups': {config.get('estimated_num_groups', '???')},")

    # Format types compactly
    params = config['param_types']
    declared = config['declared_types']

    # Compact format for params
    if len(set(params)) == 1 and params:
        lines.append(f"    'param_types': ['{params[0]}'] * {len(params)},")
    else:
        lines.append(f"    'param_types': {params},")

    # Compact format for declared types
    groups = []
    if declared:
        current = declared[0]
        count = 1
        for t in declared[1:]:
            if t == current:
                count += 1
            else:
                groups.append((count, current))
                current = t
                count = 1
        groups.append((count, current))

    if len(groups) == 1:
        lines.append(f"    'declared_types': ['{groups[0][1]}'] * {groups[0][0]},")
    elif groups:
        parts = ' + '.join(f"['{t}'] * {n}" for n, t in groups)
        lines.append(f"    'declared_types': {parts},")
    else:
        lines.append(f"    'declared_types': [],")

    lines.append(f"    'frame_pointer_local': {config['frame_pointer_local']},")
    lines.append(f"    'orig_frame_size': {config['orig_frame_size']},")

    if config['strategy'] == 'br_table':
        lines.append(f"    'opcode_local': {config.get('opcode_local')},")
        lines.append(f"    'handler_container_label': {config.get('handler_container_label')},")
        lines.append(f"    'num_structural': {config.get('num_structural')},")
        st = config.get('structural_types', {})
        lines.append(f"    'structural_types': {{")
        for k in sorted(st.keys()):
            lines.append(f"        {k}: '{st[k]}',")
        lines.append(f"    }},")

    if config['strategy'] == 'block_extraction':
        if 'extraction_depths' in config:
            lines.append(f"    'extraction_depths': {config['extraction_depths']},")
        elif 'extraction_depth' in config:
            lines.append(f"    'extraction_depth': {config['extraction_depth']},")
        lines.append(f"    'min_block_size': {config.get('min_block_size', 200)},")
        lines.append(f"    'has_result': {config.get('has_result', False)},")

    lines.append(f"}}")
    return '\n'.join(lines)


def validate_against_known(detected, known, func_index):
    """Compare detected config against a known-good config."""
    mismatches = []
    for key in ['param_types', 'declared_types', 'frame_pointer_local', 'orig_frame_size',
                'strategy', 'opcode_local', 'handler_container_label', 'num_structural',
                'extraction_depth', 'extraction_depths', 'min_block_size', 'has_result']:
        if key in known:
            detected_val = detected.get(key)
            known_val = known[key]
            if detected_val != known_val:
                mismatches.append((key, known_val, detected_val))

    # Check structural_types
    if 'structural_types' in known:
        k_st = known['structural_types']
        d_st = detected.get('structural_types', {})
        if k_st != d_st:
            mismatches.append(('structural_types', k_st, d_st))

    return mismatches


# Known-good configs for validation
KNOWN_CONFIGS = {
    482: {
        'strategy': 'br_table',
        'param_types': ['i32', 'i32'],
        'declared_types': ['i32'] * 30 + ['i64'] * 2,
        'frame_pointer_local': 14,
        'orig_frame_size': 1280,
        'opcode_local': 28,
        'handler_container_label': 8,
        'num_structural': 8,
        'structural_types': {
            1: 'loop', 2: 'loop', 3: 'block', 4: 'block',
            5: 'block', 6: 'loop', 7: 'if',
        },
    },
    180: {
        'strategy': 'br_table',
        'param_types': ['i32'],
        'declared_types': ['i32'] * 39 + ['i64'] * 6 + ['f64'] * 2,
        'frame_pointer_local': 6,
        'orig_frame_size': 512,
        'opcode_local': 1,
        'handler_container_label': 8,
        'num_structural': 8,
        'structural_types': {
            1: 'block', 2: 'block', 3: 'block', 4: 'block',
            5: 'block', 6: 'loop', 7: 'block',
        },
    },
    1345: {
        'strategy': 'br_table',
        'param_types': ['i32'] * 3,
        'declared_types': ['i32'] * 18,
        'frame_pointer_local': 6,
        'orig_frame_size': 144,
        'opcode_local': 4,
        'handler_container_label': 32,
        'num_structural': 32,
        'structural_types': {
            1: 'block', 2: 'block', 3: 'block', 4: 'block',
            5: 'block', 6: 'block', 7: 'block', 8: 'block',
            9: 'block', 10: 'block', 11: 'block', 12: 'block',
            13: 'block', 14: 'block', 15: 'block', 16: 'block',
            17: 'block', 18: 'block', 19: 'block', 20: 'block',
            21: 'block', 22: 'block', 23: 'block', 24: 'block',
            25: 'block', 26: 'block', 27: 'block', 28: 'block',
            29: 'block', 30: 'block', 31: 'loop',
        },
    },
    1177: {
        'strategy': 'block_extraction',
        'param_types': ['i32'] * 6,
        'declared_types': ['i32'] * 40 + ['i64'] * 1,
        'frame_pointer_local': 8,
        'orig_frame_size': 336,
        'extraction_depth': 4,
        'min_block_size': 200,
        'has_result': False,
    },
}


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Auto-detect splitting config from WAT')
    parser.add_argument('wat_file', nargs='?', default='wasm-lib/libsqlite3_orig.wat')
    parser.add_argument('func_index', nargs='?', type=int, default=None)
    parser.add_argument('--all', action='store_true', help='Detect all functions >2500 WAT lines')
    parser.add_argument('--validate', action='store_true', help='Validate against known configs')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    args = parser.parse_args()

    print(f"Loading WAT: {args.wat_file}")
    with open(args.wat_file) as f:
        wat_lines = f.readlines()
    print(f"  {len(wat_lines)} lines")

    if args.validate:
        print("\n=== Validating against known configs ===\n")
        all_pass = True
        for func_idx, known in sorted(KNOWN_CONFIGS.items()):
            print(f"func_{func_idx}:")
            detected = auto_detect_config(wat_lines, func_idx)
            if detected is None:
                print(f"  ERROR: function not found in WAT")
                all_pass = False
                continue

            mismatches = validate_against_known(detected, known, func_idx)
            if mismatches:
                all_pass = False
                for key, expected, got in mismatches:
                    print(f"  MISMATCH {key}:")
                    print(f"    expected: {expected}")
                    print(f"    got:      {got}")
            else:
                print(f"  OK - all fields match")

        print(f"\n{'ALL PASSED' if all_pass else 'SOME FAILED'}")
        return

    if args.func_index is not None:
        config = auto_detect_config(wat_lines, args.func_index)
        if config is None:
            print(f"Function {args.func_index} not found")
            sys.exit(1)
        if args.json:
            print(json.dumps(config, indent=2))
        else:
            print(f"\n{format_config_as_python(config)}")
            print(f"\n# WAT lines: {config['wat_lines']}")
            print(f"# Estimated bytecode: ~{config['wat_lines'] * 3} bytes")
            if 'num_handlers' in config:
                print(f"# br_table handlers: {config['num_handlers']}")
            print(f"# Total locals: {len(config['param_types']) + len(config['declared_types'])}")
        return

    if args.all:
        from correlate_wat_bytecode import count_wat_lines_per_func
        func_sizes = count_wat_lines_per_func(args.wat_file)
        candidates = [(idx, lines) for idx, lines in func_sizes.items() if lines > 2500]
        candidates.sort(key=lambda x: -x[1])

        print(f"\n=== Functions >2500 WAT lines ({len(candidates)} candidates) ===\n")
        for func_idx, wat_line_count in candidates:
            config = auto_detect_config(wat_lines, func_idx)
            if config:
                strategy = config['strategy']
                locals_count = len(config['param_types']) + len(config['declared_types'])
                handlers = config.get('num_handlers', '-')
                est_bc = wat_line_count * 3
                groups = config.get('estimated_num_groups', '-')
                if strategy == 'br_table':
                    detail = f"handlers={handlers}, groups={groups}"
                else:
                    depths = config.get('extraction_depths',
                                        [config.get('extraction_depth', '?')])
                    detail = f"depths={depths}, min_size={config.get('min_block_size', '?')}"
                print(f"  func_{func_idx:>5}: {wat_line_count:>6} WAT lines, "
                      f"~{est_bc:>6}B est, {strategy:>18}, "
                      f"{locals_count:>3} locals, {detail}")
        return

    print("Usage: specify --validate, --all, or a func_index")


if __name__ == '__main__':
    main()
