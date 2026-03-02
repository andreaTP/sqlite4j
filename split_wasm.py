#!/usr/bin/env python3
"""
Wasm → Wasm transformation: split large functions into thin dispatchers
plus helper/group functions so that each piece stays under the HotSpot
C2 JIT 8KB threshold.

Two strategies are supported:
  A) br_table dispatch  — for funcs that use a br_table switch (180, 482)
  B) block extraction   — for funcs with large nested blocks (1177, 1194)

Usage:
  python3 split_wasm.py wasm-lib/libsqlite3.wasm wasm-lib/libsqlite3_split.wasm
"""

import re
import subprocess
import sys
import os
import tempfile

# ─── Per-function configurations ─────────────────────────────────────────────

# Local type descriptors: (count, type_string)
# Listed in declaration order (matches the (local ...) line in WAT)

FUNC_482_CONFIG = {
    'func_index': 482,
    'strategy': 'br_table',
    'num_groups': 8,
    # Locals: 2 i32 params + 30 i32 declared + 2 i64 declared = 34 total
    'param_types': ['i32', 'i32'],
    'declared_types': ['i32'] * 30 + ['i64'] * 2,
    'frame_pointer_local': 14,
    'orig_frame_size': 1280,
    'opcode_local': 28,       # local set by local.tee before br_table
    'handler_container_label': 8,  # @8 is the handler container
    'num_structural': 8,      # labels @1-@8
    # Structural label types (from outermost @1 to innermost @7, excluding @8)
    'structural_types': {
        1: 'loop', 2: 'loop', 3: 'block', 4: 'block',
        5: 'block', 6: 'loop', 7: 'if',
    },
}

FUNC_180_CONFIG = {
    'func_index': 180,
    'strategy': 'br_table',
    'num_groups': 12,
    # Locals: 1 i32 param + 39 i32 + 6 i64 + 2 f64 = 48 total
    'param_types': ['i32'],
    'declared_types': ['i32'] * 39 + ['i64'] * 6 + ['f64'] * 2,
    'frame_pointer_local': 6,
    'orig_frame_size': 512,
    'opcode_local': 1,        # local set by local.tee before br_table
    'handler_container_label': 8,
    'num_structural': 8,
    'structural_types': {
        1: 'block', 2: 'block', 3: 'block', 4: 'block',
        5: 'block', 6: 'loop', 7: 'block',
    },
}

FUNC_1177_CONFIG = {
    'func_index': 1177,
    'strategy': 'block_extraction',
    # Locals: 6 i32 params + 40 i32 + 1 i64 = 47 total
    'param_types': ['i32'] * 6,
    'declared_types': ['i32'] * 40 + ['i64'] * 1,
    'frame_pointer_local': 8,
    'orig_frame_size': 336,
    'extraction_depth': 4,         # extract blocks at depth 4
    'min_block_size': 200,         # only extract blocks >= 200 lines
    'has_result': False,           # void return
}

FUNC_1194_CONFIG = {
    'func_index': 1194,
    'strategy': 'block_extraction',
    # Locals: 3 i32 params + 35 i32 + 3 i64 = 41 total
    'param_types': ['i32'] * 3,
    'declared_types': ['i32'] * 35 + ['i64'] * 3,
    'frame_pointer_local': 5,
    'orig_frame_size': 368,
    'extraction_depths': [7, 8, 13],  # extract blocks at multiple depths
    'min_block_size': 200,            # only extract blocks >= 200 lines
    'max_block_size': 4000,           # don't extract blocks > 4000 lines (too large for helpers)
}

FUNC_1345_CONFIG = {
    'func_index': 1345,
    'strategy': 'br_table',
    'num_groups': 2,
    # Locals: 3 i32 params + 18 i32 declared = 21 total
    'param_types': ['i32'] * 3,
    'declared_types': ['i32'] * 18,
    'frame_pointer_local': 6,
    'orig_frame_size': 144,
    'opcode_local': 4,        # local set by local.tee before br_table
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
}

ALL_CONFIGS = [FUNC_482_CONFIG, FUNC_180_CONFIG, FUNC_1177_CONFIG]

# ─── Shared Infrastructure ───────────────────────────────────────────────────

def all_local_types(config):
    """Return list of all local types (params + declared)."""
    return config['param_types'] + config['declared_types']


def compute_spill_layout(config):
    """Compute spill area layout: base offset, per-local offsets, total size.

    Returns (spill_base, local_offsets, new_frame_size) where:
      - spill_base: byte offset where spill area starts
      - local_offsets: list of (offset, type) for each local
      - new_frame_size: extended frame size (rounded up to 16-byte alignment)
    """
    spill_base = config['orig_frame_size']
    types = all_local_types(config)
    offsets = []
    current = spill_base
    for t in types:
        offsets.append((current, t))
        if t in ('i32', 'f32'):
            current += 4
        else:  # i64, f64
            current += 8
    # Reserve space for structural result slot (8 bytes, handles any type)
    struct_result_offset = current
    current += 8
    # Round up to 16-byte alignment
    new_frame_size = (current + 15) & ~15
    return spill_base, offsets, new_frame_size, struct_result_offset


def generate_locals_declaration(types):
    """Generate a WAT locals declaration string from a list of type strings."""
    return '    (local ' + ' '.join(types) + ')\n'


def type_size(t):
    """Return byte size of a WASM type."""
    return 4 if t in ('i32', 'f32') else 8


def load_op(t):
    """Return the WASM load instruction for a type."""
    return {'i32': 'i32.load', 'i64': 'i64.load',
            'f32': 'f32.load', 'f64': 'f64.load'}[t]


def store_op(t):
    """Return the WASM store instruction for a type."""
    return {'i32': 'i32.store', 'i64': 'i64.store',
            'f32': 'f32.store', 'f64': 'f64.store'}[t]


def generate_spill_main(config, indent='    '):
    """Generate code to store all locals to spill area (in the main function)."""
    _, local_offsets, _, _ = compute_spill_layout(config)
    fp = config['frame_pointer_local']
    lines = []
    for i, (offset, t) in enumerate(local_offsets):
        lines.append(f'{indent}local.get {fp}\n')
        lines.append(f'{indent}local.get {i}\n')
        lines.append(f'{indent}{store_op(t)} offset={offset}\n')
    return lines


def generate_reload_main(config, indent='    '):
    """Generate code to reload all locals from spill area (in the main function)."""
    _, local_offsets, _, _ = compute_spill_layout(config)
    fp = config['frame_pointer_local']
    lines = []
    for i, (offset, t) in enumerate(local_offsets):
        lines.append(f'{indent}local.get {fp}\n')
        lines.append(f'{indent}{load_op(t)} offset={offset}\n')
        lines.append(f'{indent}local.set {i}\n')
    return lines


def generate_load_locals_helper(config, param_offset, indent='    '):
    """Generate code to load all original locals from spill area in a helper.

    param_offset: number of helper function params (locals 0..param_offset-1
    are params, so original local i maps to helper local i+param_offset).
    """
    _, local_offsets, _, _ = compute_spill_layout(config)
    lines = []
    for i, (offset, t) in enumerate(local_offsets):
        lines.append(f'{indent}local.get 0\n')  # $frame (always param 0)
        lines.append(f'{indent}{load_op(t)} offset={offset}\n')
        lines.append(f'{indent}local.set {i + param_offset}\n')
    return lines


def generate_store_locals_helper(config, param_offset, indent='    '):
    """Generate code to store all original locals back to spill area in a helper."""
    _, local_offsets, _, _ = compute_spill_layout(config)
    lines = []
    for i, (offset, t) in enumerate(local_offsets):
        lines.append(f'{indent}local.get 0\n')  # $frame
        lines.append(f'{indent}local.get {i + param_offset}\n')
        lines.append(f'{indent}{store_op(t)} offset={offset}\n')
    return lines


def find_func_boundaries(wat_lines, func_index):
    """Find start and end line of a function in WAT (0-indexed, exclusive end)."""
    func_start = None
    for i, line in enumerate(wat_lines):
        if line.strip().startswith(f'(func (;{func_index};)'):
            func_start = i
        elif func_start is not None and (
            line.strip().startswith(f'(func (;{func_index + 1};)') or
            line.strip().startswith('(table') or
            line.strip().startswith('(memory') or
            line.strip().startswith('(global')
        ):
            return func_start, i
    if func_start is not None:
        # Last function — find the closing )
        for i in range(len(wat_lines) - 1, func_start, -1):
            if wat_lines[i].strip() == ')':
                return func_start, i
    raise ValueError(f'Could not find func {func_index}')


def find_last_func_index(wat_lines):
    """Find the highest function index in the WAT."""
    last = None
    for line in reversed(wat_lines):
        m = re.match(r'\s*\(func \(;(\d+);\)', line)
        if m:
            last = int(m.group(1))
            break
    return last


def count_module_funcs(wat_lines):
    """Count module functions in WAT."""
    return sum(1 for line in wat_lines if re.match(r'\s*\(func \(;\d+;\)', line))


def find_or_create_type(wat_lines, param_types, result_types):
    """Find an existing type matching the signature, or return the index for a new one.

    Returns (type_index, new_type_line_or_None).
    """
    # Build the signature string
    params = ' '.join(f'(param {t})' for t in param_types) if param_types else ''
    results = ' '.join(f'(result {t})' for t in result_types) if result_types else ''

    # Also try the compact form: (param i32 i32)
    compact_params = f'(param {" ".join(param_types)})' if param_types else ''
    compact_results = f'(result {" ".join(result_types)})' if result_types else ''

    last_type_idx = -1
    last_type_line = -1
    for i, line in enumerate(wat_lines):
        m = re.match(r'\s*\(type \(;(\d+);\) \(func (.*)\)\)', line)
        if m:
            idx = int(m.group(1))
            sig = m.group(2).strip()
            last_type_idx = max(last_type_idx, idx)
            last_type_line = i

            # Check if signature matches
            # Normalize: remove extra spaces
            sig_normalized = ' '.join(sig.split())

            target_sigs = []
            if params and results:
                target_sigs.append(f'{params} {results}')
                target_sigs.append(f'{compact_params} {compact_results}')
            elif params:
                target_sigs.append(params)
                target_sigs.append(compact_params)
            elif results:
                target_sigs.append(results)
                target_sigs.append(compact_results)
            else:
                target_sigs.append('')

            for target in target_sigs:
                if sig_normalized == ' '.join(target.split()):
                    return idx, None

    # Need to create a new type
    new_idx = last_type_idx + 1
    sig_parts = []
    if compact_params:
        sig_parts.append(compact_params)
    if compact_results:
        sig_parts.append(compact_results)
    sig_str = ' '.join(sig_parts)
    new_line = f'  (type (;{new_idx};) (func {sig_str}))\n'
    return new_idx, new_line


# ─── Strategy A: br_table dispatch splitting ─────────────────────────────────

def parse_brtable_func(wat_lines, func_start, func_end, config):
    """Parse a function with br_table dispatch pattern."""
    func_lines = wat_lines[func_start:func_end]
    handler_container = config['handler_container_label']

    # Find the main br_table (the longest one)
    br_table_line = None
    br_table_len = 0
    for i, line in enumerate(func_lines):
        stripped = line.strip()
        if stripped.startswith('br_table') and len(stripped) > br_table_len:
            br_table_line = i
            br_table_len = len(stripped)

    if br_table_line is None:
        raise ValueError(f'Could not find br_table in func {config["func_index"]}')

    # Parse br_table entries
    br_line = func_lines[br_table_line].strip()
    entries = re.findall(r'(\d+)\s*\(;@\d+;\)', br_line)
    br_table_entries = [int(e) for e in entries]

    # Build label stack at br_table position, capturing result types
    label_stack = []
    for i in range(len(func_lines)):
        stripped = func_lines[i].strip()
        if i < 2:
            continue
        label_match = re.search(r';; label = @(\d+)', stripped)
        result_match = re.search(r'\(result (\w+)\)', stripped)
        if stripped.startswith(('block', 'loop', 'if')) and not stripped.startswith(('block)', 'loop)', 'if)')):
            kind = stripped.split()[0].split('(')[0]
            label = int(label_match.group(1)) if label_match else -1
            result_type = result_match.group(1) if result_match else None
            label_stack.append((kind, label, i, result_type))
        elif stripped == 'end' or (stripped.startswith('end') and not stripped.startswith('end)')):
            if label_stack:
                label_stack.pop()
        if i == br_table_line:
            break

    orig_depth_at_brtable = len(label_stack)
    depth_at_brtable = orig_depth_at_brtable
    num_dispatch_blocks = depth_at_brtable - handler_container

    # Find handler boundaries (note: depth_at_brtable is decremented during this loop)
    handlers = []
    handler_start_line = br_table_line + 1
    d = depth_at_brtable

    for i in range(br_table_line + 1, len(func_lines)):
        stripped = func_lines[i].strip()
        if stripped.startswith(('block', 'loop', 'if')) and not stripped.startswith(('block)', 'loop)', 'if)')):
            d += 1
        elif stripped == 'end' or (stripped.startswith('end') and not stripped.startswith('end)')):
            d -= 1
            if d < depth_at_brtable:
                handler_code = func_lines[handler_start_line:i]
                handlers.append({
                    'index': len(handlers),
                    'start': handler_start_line,
                    'end': i,
                    'code': handler_code,
                })
                handler_start_line = i + 1
                depth_at_brtable -= 1

    num_opcode_handlers = num_dispatch_blocks + 1
    opcode_handlers = handlers[:num_opcode_handlers]
    structural_code = handlers[num_opcode_handlers:]

    # Build map: handler_index → result type of the dispatch block that WRAPS it
    # Use orig_depth_at_brtable (before handler loop modified depth_at_brtable).
    # label_stack[orig_depth_at_brtable - 1 - h] = dispatch block whose end
    # creates handler h's boundary. Its result type flows to handler h+1.
    dispatch_block_result_types = {}
    for h in range(num_opcode_handlers):
        stack_pos = orig_depth_at_brtable - 1 - h
        if stack_pos >= handler_container and stack_pos < orig_depth_at_brtable:
            _, _, _, result_type = label_stack[stack_pos]
            if result_type:
                # Key by h+1: the handler that CONSUMES the result
                dispatch_block_result_types[h + 1] = result_type

    if dispatch_block_result_types:
        print(f'  Dispatch blocks with result types: {dispatch_block_result_types}')

    # Find block @handler_container start
    block_container_start = None
    for idx, (kind, label, line, _rt) in enumerate(label_stack):
        if label == handler_container and idx == handler_container - 1:
            block_container_start = (kind, label, line)
            break

    if block_container_start is None:
        raise ValueError(f'Could not find block @{handler_container}')

    print(f'  br_table at line {br_table_line + 1} (within func)')
    print(f'  {len(br_table_entries)} br_table entries')
    # Extract structural label result types (labels @1 through @handler_container-1)
    structural_result_types = {}
    for pos in range(handler_container):
        kind, label, line, result_type = label_stack[pos]
        if result_type:
            structural_result_types[label] = result_type

    if structural_result_types:
        print(f'  Structural labels with result types: {structural_result_types}')

    print(f'  {num_dispatch_blocks} dispatch blocks')
    print(f'  {len(opcode_handlers)} opcode handlers')
    print(f'  {len(structural_code)} structural code regions')

    return {
        'func_lines': func_lines,
        'br_table_line': br_table_line,
        'br_table_entries': br_table_entries,
        'opcode_handlers': opcode_handlers,
        'structural_code': structural_code,
        'label_stack': label_stack,
        'depth_at_brtable': orig_depth_at_brtable,
        'num_dispatch_blocks': num_dispatch_blocks,
        'block_container_start_line': block_container_start[2],
        'dispatch_block_result_types': dispatch_block_result_types,
        'structural_result_types': structural_result_types,
    }


def transform_handler_code(handler_code, handler_orig_index, group_start, group_end,
                           config, result_local_idx, typed_dispatch_temps=None,
                           structural_result_types=None):
    """Transform handler code for use in a group function.

    Generalizes local offset, branch depth, and structural exit handling.
    typed_dispatch_temps: dict mapping handler_idx → (result_type, temp_local_idx)
                          for dispatch blocks that originally had result types.
    structural_result_types: dict mapping structural label → result type (e.g. {1: 'i32'})
    """
    if typed_dispatch_temps is None:
        typed_dispatch_temps = {}
    if structural_result_types is None:
        structural_result_types = {}
    all_types = all_local_types(config)
    num_orig_locals = len(all_types)
    num_dispatch_blocks = config['_num_dispatch_blocks']
    handler_container = config['handler_container_label']
    num_structural = config['num_structural']
    _, _, _, struct_result_offset = compute_spill_layout(config)
    param_offset = 2  # group functions have 2 params: $frame, $handler_idx

    H = handler_orig_index
    H_local = H - group_start
    G_size = group_end - group_start

    orig_depth_to_container = num_dispatch_blocks - H

    internal_depth = 0

    transformed = []
    for line in handler_code:
        stripped = line.strip()

        # Track internal nesting
        if stripped.startswith(('block', 'loop', 'if')) and not stripped.startswith(('block)', 'loop)', 'if)')):
            internal_depth += 1

        # Transform local.get/set/tee
        local_match = re.match(r'^(\s*)(local\.(get|set|tee))\s+(\d+)(.*)', line.rstrip())
        if local_match:
            indent = local_match.group(1)
            op = local_match.group(2)
            idx = int(local_match.group(4))
            rest = local_match.group(5)
            new_idx = idx + param_offset
            transformed.append(f'{indent}{op} {new_idx}{rest}\n')
            if stripped.startswith('end'):
                internal_depth -= 1
            continue

        # Transform br N (;@M;) and br_if N (;@M;)
        br_match = re.match(r'^(\s*)(br_if|br)\s+(\d+)\s*\(;@(\d+);\)(.*)', line.rstrip())
        if br_match:
            indent = br_match.group(1)
            op = br_match.group(2)
            old_depth = int(br_match.group(3))
            target_label = int(br_match.group(4))
            rest = br_match.group(5)

            if old_depth < internal_depth:
                transformed.append(line)
            else:
                effective_external = old_depth - internal_depth

                if effective_external < G_size - 1 - H_local:
                    # Within-group dispatch block
                    # Check if the target dispatch block is typed
                    target_handler = H + effective_external + 1
                    if target_handler in typed_dispatch_temps:
                        rt, temp_idx = typed_dispatch_temps[target_handler]
                        if op == 'br':
                            # Save typed value to temp before branching
                            transformed.append(f'{indent}local.set {temp_idx} ;; save {rt} for typed dispatch\n')
                            transformed.append(line)
                        else:  # br_if
                            # Stack: [..., T, i32]. Use if/else:
                            # if branch: save T, br to dispatch block
                            # else branch: T passes through unchanged
                            new_depth = old_depth + 1  # +1 for the if block
                            transformed.append(f'{indent}if ;; typed br_if\n')
                            transformed.append(f'{indent}  local.set {temp_idx} ;; save {rt}\n')
                            transformed.append(f'{indent}  br {new_depth}\n')
                            transformed.append(f'{indent}else\n')
                            transformed.append(f'{indent}end\n')
                    else:
                        transformed.append(line)
                elif effective_external == orig_depth_to_container:
                    # Was @handler_container (continue)
                    new_depth = internal_depth + (G_size - 1 - H_local)
                    transformed.append(f'{indent}{op} {new_depth} ;; @{handler_container}→$continue\n')
                elif effective_external > orig_depth_to_container:
                    # Structural target
                    structural_offset = effective_external - orig_depth_to_container
                    structural_label = handler_container - structural_offset
                    cont_code = structural_label  # code = label number (1-7)
                    new_depth_exit = internal_depth + (G_size - H_local)

                    # Check if the structural label has a result type
                    srt = structural_result_types.get(structural_label)

                    if op == 'br':
                        if srt:
                            # Save the structural result value to the spill area
                            # Stack: [..., result_value]. Use result_local as temp.
                            transformed.append(f'{indent}local.set {result_local_idx} ;; temp save {srt} result\n')
                            transformed.append(f'{indent}local.get 0 ;; frame ptr\n')
                            transformed.append(f'{indent}local.get {result_local_idx}\n')
                            transformed.append(f'{indent}{store_op(srt)} offset={struct_result_offset}\n')
                        transformed.append(f'{indent}i32.const {cont_code}\n')
                        transformed.append(f'{indent}local.set {result_local_idx}\n')
                        transformed.append(f'{indent}br {new_depth_exit} ;; @{structural_label}→$exit\n')
                    else:  # br_if
                        if srt:
                            # Stack: [..., result_value, i32_cond]
                            # Use if/else to save the result only when branch is taken
                            transformed.append(f'{indent}if ;; br_if to typed @{structural_label}\n')
                            # Inside if: stack has [..., result_value]
                            transformed.append(f'{indent}  local.set {result_local_idx} ;; temp save {srt}\n')
                            transformed.append(f'{indent}  local.get 0 ;; frame ptr\n')
                            transformed.append(f'{indent}  local.get {result_local_idx}\n')
                            transformed.append(f'{indent}  {store_op(srt)} offset={struct_result_offset}\n')
                            transformed.append(f'{indent}  i32.const {cont_code}\n')
                            transformed.append(f'{indent}  local.set {result_local_idx}\n')
                            transformed.append(f'{indent}  br {new_depth_exit + 1} ;; @{structural_label}→$exit\n')
                            transformed.append(f'{indent}else\n')
                            # else: result_value stays on stack (not taken)
                            transformed.append(f'{indent}end\n')
                        else:
                            transformed.append(f'{indent}if\n')
                            transformed.append(f'{indent}  i32.const {cont_code}\n')
                            transformed.append(f'{indent}  local.set {result_local_idx}\n')
                            transformed.append(f'{indent}  br {new_depth_exit + 1} ;; @{structural_label}→$exit (+1 for if)\n')
                        transformed.append(f'{indent}end\n')
                else:
                    # Out-of-group dispatch block → cross-group jump
                    target_handler_idx = H + effective_external + 1
                    cont_code = 100 + target_handler_idx
                    new_depth_exit = internal_depth + (G_size - H_local)

                    if op == 'br':
                        transformed.append(f'{indent}i32.const {cont_code}\n')
                        transformed.append(f'{indent}local.set {result_local_idx}\n')
                        transformed.append(f'{indent}br {new_depth_exit} ;; cross-group handler {target_handler_idx}→$exit\n')
                    else:
                        transformed.append(f'{indent}if\n')
                        transformed.append(f'{indent}  i32.const {cont_code}\n')
                        transformed.append(f'{indent}  local.set {result_local_idx}\n')
                        transformed.append(f'{indent}  br {new_depth_exit + 1} ;; cross-group handler {target_handler_idx}→$exit (+1 for if)\n')
                        transformed.append(f'{indent}end\n')

            if stripped.startswith('end'):
                internal_depth -= 1
            continue

        # Transform br_table with mixed targets
        if stripped.startswith('br_table'):
            bt_entries = re.findall(r'(\d+)\s*\(;@(\d+);\)', stripped)
            if bt_entries:
                new_entries = []
                for depth_str, label_str in bt_entries:
                    old_d = int(depth_str)
                    if old_d < internal_depth:
                        new_entries.append(depth_str)
                    else:
                        effective_external = old_d - internal_depth
                        if effective_external < G_size - 1 - H_local:
                            new_entries.append(depth_str)
                        elif effective_external == orig_depth_to_container:
                            new_d = internal_depth + (G_size - 1 - H_local)
                            new_entries.append(str(new_d))
                        elif effective_external > orig_depth_to_container:
                            new_d = internal_depth + (G_size - H_local)
                            new_entries.append(str(new_d))
                        else:
                            new_d = internal_depth + (G_size - H_local)
                            new_entries.append(str(new_d))

                indent_str = line[:len(line) - len(line.lstrip())]
                transformed.append(f'{indent_str}br_table {" ".join(new_entries)}\n')
            else:
                transformed.append(line)

            if stripped.startswith('end'):
                internal_depth -= 1
            continue

        if stripped == 'end' or (stripped.startswith('end') and not stripped.startswith('end)')):
            internal_depth -= 1

        transformed.append(line)

    return transformed


def generate_group_function(group_id, group_start, group_end, handlers,
                            func_type_idx, func_idx, config,
                            dispatch_block_result_types=None,
                            structural_result_types=None):
    """Generate a group handler function for br_table dispatch strategy."""
    if dispatch_block_result_types is None:
        dispatch_block_result_types = {}
    g_size = group_end - group_start
    all_types = all_local_types(config)
    num_orig_locals = len(all_types)
    param_offset = 2  # $frame, $handler_idx

    # Result local: after all original locals (offset by param_offset)
    result_local_idx = param_offset + num_orig_locals

    # Build locals declaration: ALL original local types + $result i32 + temp locals
    # for dispatch block result types
    extra_locals = ['i32']  # $result

    # Add temp locals for typed dispatch blocks in this group
    # typed_dispatch_temps: handler_idx → (result_type, temp_local_idx)
    typed_dispatch_temps = {}
    for h_idx, rt in dispatch_block_result_types.items():
        if group_start <= h_idx < group_end:
            temp_idx = param_offset + num_orig_locals + len(extra_locals)
            extra_locals.append(rt)
            typed_dispatch_temps[h_idx] = (rt, temp_idx)

    helper_declared = list(all_types) + extra_locals

    lines = []
    lines.append(f'  (func (;{func_idx};) (type {func_type_idx}) (param i32 i32) (result i32)\n')
    lines.append(generate_locals_declaration(helper_declared))

    # Load all original locals from spill area
    lines.extend(generate_load_locals_helper(config, param_offset))

    # Dispatch structure — all dispatch blocks are PLAIN (no result types)
    lines.append(f'    block ;; $exit\n')
    lines.append(f'      block ;; $continue\n')
    for i in range(g_size - 1, -1, -1):
        lines.append(f'        block ;; $d_{i}\n')

    # br_table dispatch
    lines.append(f'          local.get 1\n')
    if group_start > 0:
        lines.append(f'          i32.const {group_start}\n')
        lines.append(f'          i32.sub\n')
    br_entries = ' '.join(str(i) for i in range(g_size))
    lines.append(f'          br_table {br_entries} {g_size}\n')

    # Close dispatch blocks and emit handler code
    for i in range(g_size):
        handler_idx = group_start + i
        handler = handlers[handler_idx]
        lines.append(f'        end ;; $d_{i} — handler {handler_idx}\n')

        # If this handler consumes a typed dispatch block result,
        # provide the value from the temp local
        if handler_idx in typed_dispatch_temps:
            rt, temp_idx = typed_dispatch_temps[handler_idx]
            lines.append(f'        local.get {temp_idx} ;; typed dispatch result ({rt})\n')
        transformed = transform_handler_code(
            handler['code'], group_start + i,
            group_start, group_end, config, result_local_idx,
            typed_dispatch_temps, structural_result_types
        )
        for tl in transformed:
            lines.append(f'        {tl.strip()}\n')

        # If the NEXT handler consumes a typed dispatch block result,
        # save the fallthrough value to the temp local
        next_handler_idx = handler_idx + 1
        if next_handler_idx in typed_dispatch_temps and next_handler_idx < group_end:
            rt, temp_idx = typed_dispatch_temps[next_handler_idx]
            lines.append(f'        local.set {temp_idx} ;; save {rt} fallthrough for handler {next_handler_idx}\n')

    # Close $continue — store locals and return 0
    lines.append(f'      end ;; $continue\n')
    lines.extend(generate_store_locals_helper(config, param_offset, '      '))
    lines.append(f'      i32.const 0\n')
    lines.append(f'      return\n')

    # Close $exit — store locals and return result
    lines.append(f'    end ;; $exit\n')
    lines.extend(generate_store_locals_helper(config, param_offset, '    '))
    lines.append(f'    local.get {result_local_idx}\n')
    lines.append(f'  )\n')

    return lines


def generate_modified_brtable_func(parsed, group_boundaries, new_func_indices, config):
    """Generate the modified main function with handler stubs (br_table strategy)."""
    func_lines = parsed['func_lines']
    br_table_line = parsed['br_table_line']
    opcode_handlers = parsed['opcode_handlers']
    structural_code = parsed['structural_code']
    br_table_entries = parsed['br_table_entries']
    block_container_start_line = parsed['block_container_start_line']
    num_dispatch_blocks = parsed['num_dispatch_blocks']

    _, _, new_frame_size, struct_result_offset = compute_spill_layout(config)
    orig_frame_size = config['orig_frame_size']
    fp_local = config['frame_pointer_local']
    opcode_local = config['opcode_local']
    handler_container = config['handler_container_label']
    num_structural = config['num_structural']
    all_types = all_local_types(config)
    num_orig_locals = len(all_types)
    structural_result_types = parsed.get('structural_result_types', {})

    # Add one extra local for handler_idx
    handler_idx_local = num_orig_locals

    # Compute spill offset for opcode local
    _, local_offsets, _, _ = compute_spill_layout(config)
    opcode_spill_offset = local_offsets[opcode_local][0]

    result = []

    # Line 0: func declaration (unchanged)
    result.append(func_lines[0])

    # Line 1: locals — add one more i32 for handler_idx
    result.append(generate_locals_declaration(config['declared_types'] + ['i32']))

    # Copy setup code up to block @handler_container, changing frame size
    for i in range(2, block_container_start_line):
        line = func_lines[i]
        if f'i32.const {orig_frame_size}' in line and i < block_container_start_line:
            line = line.replace(f'i32.const {orig_frame_size}', f'i32.const {new_frame_size}')
        result.append(line)

    # Spill locals before block @handler_container
    result.append('    ;; === SPILL LOCALS ===\n')
    result.extend(generate_spill_main(config))

    # Initialize handler_idx to -1 (sentinel)
    result.append(f'    i32.const -1\n')
    result.append(f'    local.set {handler_idx_local}\n')

    # Emit block @handler_container opening
    result.append(func_lines[block_container_start_line])

    # Emit dispatch blocks (unchanged)
    for i in range(block_container_start_line + 1, br_table_line):
        result.append(func_lines[i])

    # Emit br_table (unchanged)
    result.append(func_lines[br_table_line])

    # Emit handler stubs
    num_handlers = len(opcode_handlers)
    num_dispatch_stubs = num_handlers - 1

    for h_idx in range(num_dispatch_stubs):
        result.append('                  end\n')
        depth_to_container = num_dispatch_stubs - 1 - h_idx
        result.append(f'                  i32.const {h_idx + 1}\n')
        result.append(f'                  local.set {handler_idx_local}\n')
        result.append(f'                  br {depth_to_container}\n')

    # Stub for last handler (post-dispatch)
    result.append(f'                  i32.const {num_dispatch_stubs}\n')
    result.append(f'                  local.set {handler_idx_local}\n')

    # Close handler container block
    result.append(f'                end ;; @{handler_container}\n')

    # Fix opcode local in spill area (it may have been set by local.tee after spill)
    result.append(f'    local.get {fp_local}\n')
    result.append(f'    local.get {opcode_local}\n')
    result.append(f'    i32.store offset={opcode_spill_offset}\n')

    # Group dispatch with redispatch loop
    result.append('    ;; === DISPATCH TO GROUP ===\n')
    result.append(f'    loop ;; $redispatch\n')

    num_groups = len(group_boundaries)
    for g_idx in range(num_groups):
        g_start, g_end = group_boundaries[g_idx]
        func_idx = new_func_indices[g_idx]

        if g_idx < num_groups - 1:
            next_g_start = group_boundaries[g_idx + 1][0]
            result.append(f'    local.get {handler_idx_local}\n')
            result.append(f'    i32.const {next_g_start}\n')
            result.append(f'    i32.lt_u\n')
            result.append(f'    if (result i32)\n')
            result.append(f'      local.get {fp_local}\n')
            result.append(f'      local.get {handler_idx_local}\n')
            result.append(f'      call {func_idx}\n')
            result.append(f'    else\n')
        else:
            result.append(f'      local.get {fp_local}\n')
            result.append(f'      local.get {handler_idx_local}\n')
            result.append(f'      call {func_idx}\n')

    for g_idx in range(num_groups - 1):
        result.append(f'    end\n')

    result.append(f'    local.set {handler_idx_local}\n')

    # Cross-group jump handling
    result.append(f'    local.get {handler_idx_local}\n')
    result.append(f'    i32.const 100\n')
    result.append(f'    i32.ge_u\n')
    result.append(f'    if\n')
    result.append(f'      local.get {handler_idx_local}\n')
    result.append(f'      i32.const 100\n')
    result.append(f'      i32.sub\n')
    result.append(f'      local.set {handler_idx_local}\n')
    result.append(f'      br 1 ;; restart $redispatch loop\n')
    result.append(f'    end\n')
    result.append(f'    end ;; $redispatch loop\n')

    # Reload locals
    result.append('    ;; === RELOAD LOCALS ===\n')
    result.extend(generate_reload_main(config))

    # Handle continuation code
    result.append('    ;; === HANDLE CONTINUATION ===\n')
    # Number of structural labels excluding @handler_container = handler_container - 1
    S = handler_container - 1  # 7 for both func 482 and func 180
    result.append(f'    local.get {handler_idx_local}\n')
    result.append(f'    if\n')

    # Generate dispatch blocks
    for i in range(S):
        result.append(f'      {"  " * i}block\n')

    # br_table dispatch
    inner_indent = '      ' + '  ' * S
    result.append(f'{inner_indent}local.get {handler_idx_local}\n')
    result.append(f'{inner_indent}i32.const 1\n')
    result.append(f'{inner_indent}i32.sub\n')
    entries = ' '.join(str(i) for i in range(S))
    result.append(f'{inner_indent}br_table {entries} 0\n')

    # Close dispatch blocks and emit branches to structural labels
    for i in range(S):
        block_indent = '      ' + '  ' * (S - 1 - i)
        result.append(f'{block_indent}end\n')
        # After block x_{S-1-i} closes (the i-th block to close):
        #   remaining continuation blocks: x_{S-2-i}, ..., x_0 = S-1-i blocks
        #   + if block = 1
        #   + structural labels @S, @(S-1), ..., @1 = S blocks
        # depth to @j = (S-1-i) + 1 + (S - j) = 2S - i - j
        # For code (i+1) → @(i+1): depth = 2S - i - (i+1) = 2S - 2i - 1
        depth = 2 * S - 1 - 2 * i
        target_label = i + 1
        srt = structural_result_types.get(target_label)
        if srt:
            # Load structural result from spill area before branching
            result.append(f'{block_indent}local.get {fp_local}\n')
            result.append(f'{block_indent}{load_op(srt)} offset={struct_result_offset}\n')
        result.append(f'{block_indent}br {depth} ;; code {i + 1} → @{i + 1}\n')

    result.append(f'    end\n')  # close if

    # Copy remaining structural code (after handler container through end of function)
    remaining_start = opcode_handlers[-1]['end'] + 1
    for i in range(remaining_start, len(func_lines)):
        line = func_lines[i]
        if f'i32.const {orig_frame_size}' in line:
            line = line.replace(f'i32.const {orig_frame_size}', f'i32.const {new_frame_size}')
        result.append(line)

    return result


def split_brtable_func(wat_lines, config, next_func_idx):
    """Split a function using the br_table dispatch strategy.

    Returns (modified_func_lines, group_func_lines_list, next_func_idx).
    """
    func_idx = config['func_index']
    func_start, func_end = find_func_boundaries(wat_lines, func_idx)
    print(f'  func {func_idx} at lines {func_start + 1}-{func_end}')

    parsed = parse_brtable_func(wat_lines, func_start, func_end, config)

    # Store dispatch block count in config for transform_handler_code
    config['_num_dispatch_blocks'] = parsed['num_dispatch_blocks']

    num_handlers = len(parsed['opcode_handlers'])
    num_groups = config['num_groups']

    # Compute group boundaries
    handlers_per_group = num_handlers // num_groups
    group_boundaries = []
    for g in range(num_groups):
        g_start = g * handlers_per_group
        g_end = num_handlers if g == num_groups - 1 else (g + 1) * handlers_per_group
        group_boundaries.append((g_start, g_end))
        print(f'  Group {g}: handlers [{g_start}, {g_end}) = {g_end - g_start} handlers')

    # Find or verify group function type: (param i32 i32) (result i32)
    group_type_idx, new_type_line = find_or_create_type(wat_lines, ['i32', 'i32'], ['i32'])

    # Assign function indices
    new_func_indices = [next_func_idx + g for g in range(num_groups)]
    print(f'  New function indices: {new_func_indices}')

    # Generate group functions
    group_functions = []
    for g in range(num_groups):
        g_start, g_end = group_boundaries[g]
        gf = generate_group_function(
            g, g_start, g_end,
            parsed['opcode_handlers'],
            group_type_idx,
            new_func_indices[g],
            config,
            parsed['dispatch_block_result_types'],
            parsed.get('structural_result_types', {})
        )
        group_functions.append(gf)
        handler_lines = sum(len(parsed['opcode_handlers'][h]['code']) for h in range(g_start, g_end))
        print(f'  Group {g}: {len(gf)} WAT lines (from {handler_lines} handler lines)')

    # Generate modified main function
    modified = generate_modified_brtable_func(parsed, group_boundaries, new_func_indices, config)
    print(f'  Modified func: {len(modified)} lines (was {func_end - func_start})')

    return func_start, func_end, modified, group_functions, next_func_idx + num_groups, new_type_line


# ─── Strategy B: block extraction splitting ──────────────────────────────────

def parse_block_structure(func_lines):
    """Parse the full block structure of a function.

    Returns a list of block descriptors:
    [{kind, label, start_line, end_line, depth, children, parent_idx}]
    """
    blocks = []
    stack = []  # (block_index, label)

    for i in range(len(func_lines)):
        stripped = func_lines[i].strip()
        if i < 2:
            continue

        label_match = re.search(r';; label = @(\d+)', stripped)

        if stripped.startswith(('block', 'loop', 'if')) and not stripped.startswith(('block)', 'loop)', 'if)')):
            kind = stripped.split()[0].split('(')[0]
            label = int(label_match.group(1)) if label_match else -1
            depth = len(stack) + 1  # depth 1 for outermost
            parent = stack[-1][0] if stack else -1
            block_idx = len(blocks)
            blocks.append({
                'kind': kind,
                'label': label,
                'start_line': i,
                'end_line': None,
                'depth': depth,
                'children': [],
                'parent_idx': parent,
            })
            if parent >= 0:
                blocks[parent]['children'].append(block_idx)
            stack.append((block_idx, label))

        elif stripped == 'end' or (stripped.startswith('end') and not stripped.startswith('end)')):
            if stack:
                block_idx, _ = stack.pop()
                blocks[block_idx]['end_line'] = i

    return blocks


def find_extraction_targets(blocks, target_depths, min_size, func_lines, max_size=None):
    """Find blocks at the target depth(s) that are large enough to extract.

    target_depths: int or list of ints — depth(s) to search for extraction targets.
    max_size: if set, exclude blocks larger than this (they'd be too large as helpers).

    Returns list of block indices (into the blocks array) sorted by start_line.
    """
    if isinstance(target_depths, int):
        target_depths = [target_depths]
    targets = []
    for idx, b in enumerate(blocks):
        if b['depth'] in target_depths and b['end_line'] is not None:
            size = b['end_line'] - b['start_line']
            if size >= min_size and (max_size is None or size <= max_size):
                targets.append(idx)

    # Remove descendants when an ancestor is also selected.
    # If A contains B, keep A (the ancestor) and remove B — extracting A
    # already removes B's code from the main function.
    to_remove = set()
    for i, t1 in enumerate(targets):
        for j, t2 in enumerate(targets):
            if i == j:
                continue
            # Check if t1 is an ancestor of t2 → remove t2 (descendant)
            parent = blocks[t2]['parent_idx']
            while parent >= 0:
                if parent == t1:
                    to_remove.add(j)
                    break
                parent = blocks[parent]['parent_idx']
    targets = [t for i, t in enumerate(targets) if i not in to_remove]

    targets.sort(key=lambda i: blocks[i]['start_line'])
    return targets


def transform_extracted_block_code(block_code, config, param_offset, exit_labels_info,
                                    skip_local_transform=False):
    """Transform code extracted from a block for use in a helper function.

    exit_labels_info: list of (original_depth_to_label, continuation_code) for
                      branches that exit the extracted block to enclosing labels.
                      Sorted by original_depth (ascending = innermost first).
                      The depth is relative to the extracted block's interior
                      (i.e., depth 0 = the extracted block itself).
    skip_local_transform: if True, don't offset local indices (used for sub-splitting
                          where code is already offset from a parent helper).
    """
    # The extracted code is the INTERIOR of a block (between block/end).
    # In the helper, this code is wrapped in:
    #   block $exit        (depth from code = extracted_block_nesting + 1)
    #     block $continue  (depth from code = extracted_block_nesting)
    #       <code here>
    #     end $continue
    #     store locals, return 0
    #   end $exit
    #   store locals, return result

    # But actually, the code is placed directly (not re-wrapped in a block matching
    # the original). So branches to the extracted block itself = $continue.
    # Branches to parents of the extracted block = set continuation + br $exit.

    internal_depth = 0
    transformed = []

    for line in block_code:
        stripped = line.strip()

        # Track internal nesting
        if stripped.startswith(('block', 'loop', 'if')) and not stripped.startswith(('block)', 'loop)', 'if)')):
            internal_depth += 1

        # Transform local.get/set/tee
        local_match = re.match(r'^(\s*)(local\.(get|set|tee))\s+(\d+)(.*)', line.rstrip())
        if local_match:
            if skip_local_transform:
                transformed.append(line)
                if stripped.startswith('end'):
                    internal_depth -= 1
                continue
            indent = local_match.group(1)
            op = local_match.group(2)
            idx = int(local_match.group(4))
            rest = local_match.group(5)
            new_idx = idx + param_offset
            transformed.append(f'{indent}{op} {new_idx}{rest}\n')
            if stripped.startswith('end'):
                internal_depth -= 1
            continue

        # Transform br N (;@M;) and br_if N (;@M;)
        br_match = re.match(r'^(\s*)(br_if|br)\s+(\d+)\s*\(;@(\d+);\)(.*)', line.rstrip())
        if br_match:
            indent = br_match.group(1)
            op = br_match.group(2)
            old_depth = int(br_match.group(3))
            target_label = int(br_match.group(4))
            rest = br_match.group(5)

            if old_depth < internal_depth:
                # Internal branch — keep as-is
                transformed.append(line)
            else:
                effective_external = old_depth - internal_depth
                # effective_external 0 = the extracted block itself (→ $continue)
                # effective_external 1+ = enclosing blocks (need continuation codes)

                handled = False
                for orig_depth, cont_code in exit_labels_info:
                    if effective_external == orig_depth:
                        if cont_code == 0:
                            # This is the extracted block's own exit → $continue
                            new_depth = internal_depth + 0  # $continue is at offset 0 from code
                            transformed.append(f'{indent}{op} {new_depth} ;; →$continue\n')
                        else:
                            result_local = exit_labels_info[-1][1]  # Not right, need result local
                            # Set continuation code and branch to $exit
                            new_depth_exit = internal_depth + 1  # $exit is at offset 1 from code

                            if op == 'br':
                                transformed.append(f'{indent}i32.const {cont_code}\n')
                                transformed.append(f'{indent}local.set {param_offset + len(all_local_types(config))}\n')
                                transformed.append(f'{indent}br {new_depth_exit} ;; →$exit (cont={cont_code})\n')
                            else:
                                transformed.append(f'{indent}if\n')
                                transformed.append(f'{indent}  i32.const {cont_code}\n')
                                transformed.append(f'{indent}  local.set {param_offset + len(all_local_types(config))}\n')
                                transformed.append(f'{indent}  br {new_depth_exit + 1} ;; →$exit (+1 for if) (cont={cont_code})\n')
                                transformed.append(f'{indent}end\n')
                        handled = True
                        break

                if not handled:
                    # Unknown exit — treat as continuation to outermost
                    max_cont = max(c for _, c in exit_labels_info)
                    new_depth_exit = internal_depth + 1
                    if op == 'br':
                        transformed.append(f'{indent}i32.const {max_cont + 1}\n')
                        transformed.append(f'{indent}local.set {param_offset + len(all_local_types(config))}\n')
                        transformed.append(f'{indent}br {new_depth_exit} ;; →$exit (unknown)\n')
                    else:
                        transformed.append(f'{indent}if\n')
                        transformed.append(f'{indent}  i32.const {max_cont + 1}\n')
                        transformed.append(f'{indent}  local.set {param_offset + len(all_local_types(config))}\n')
                        transformed.append(f'{indent}  br {new_depth_exit + 1} ;; →$exit (unknown, +1 for if)\n')
                        transformed.append(f'{indent}end\n')

            if stripped.startswith('end'):
                internal_depth -= 1
            continue

        # Transform br_table
        if stripped.startswith('br_table'):
            bt_entries = re.findall(r'(\d+)\s*\(;@(\d+);\)', stripped)
            if bt_entries:
                new_entries = []
                for depth_str, label_str in bt_entries:
                    old_d = int(depth_str)
                    if old_d < internal_depth:
                        new_entries.append(depth_str)
                    else:
                        effective_external = old_d - internal_depth
                        matched = False
                        for orig_depth, cont_code in exit_labels_info:
                            if effective_external == orig_depth:
                                if cont_code == 0:
                                    new_d = internal_depth + 0
                                else:
                                    new_d = internal_depth + 1  # $exit
                                new_entries.append(str(new_d))
                                matched = True
                                break
                        if not matched:
                            new_d = internal_depth + 1  # $exit
                            new_entries.append(str(new_d))

                indent_str = line[:len(line) - len(line.lstrip())]
                transformed.append(f'{indent_str}br_table {" ".join(new_entries)}\n')
            else:
                transformed.append(line)
            if stripped.startswith('end'):
                internal_depth -= 1
            continue

        if stripped == 'end' or (stripped.startswith('end') and not stripped.startswith('end)')):
            internal_depth -= 1

        transformed.append(line)

    return transformed


def compute_exit_labels_for_block(blocks, block_idx, func_lines):
    """Compute exit label info for an extracted block.

    Returns list of (effective_depth, continuation_code) tuples.
    effective_depth 0 = the block itself, 1 = parent, etc.
    continuation_code 0 = fall-through ($continue), 1+ = structural exit.
    """
    result = [(0, 0)]  # depth 0 = self = $continue

    # Walk up parents — each enclosing block/loop/if gets a continuation code
    parent_idx = blocks[block_idx]['parent_idx']
    depth = 1
    cont_code = 1
    while parent_idx >= 0:
        result.append((depth, cont_code))
        parent_idx = blocks[parent_idx]['parent_idx']
        depth += 1
        cont_code += 1

    return result


def generate_block_helper_function(func_idx, type_idx, config, block_code,
                                   exit_labels_info, param_offset,
                                   skip_local_transform=False):
    """Generate a helper function for an extracted block.

    skip_local_transform: if True, don't offset local indices in the code
                          (used for sub-splitting where code is already offset).
    """
    all_types = all_local_types(config)
    num_orig_locals = len(all_types)
    result_local_idx = param_offset + num_orig_locals

    # Declared locals = ALL original local types + 1 i32 for $result
    helper_declared = list(all_local_types(config)) + ['i32']

    lines = []
    # Helper signature: (param i32) → (result i32)  — just $frame pointer
    lines.append(f'  (func (;{func_idx};) (type {type_idx}) (param i32) (result i32)\n')
    lines.append(generate_locals_declaration(helper_declared))

    # Load locals from spill area
    lines.extend(generate_load_locals_helper(config, param_offset, '    '))

    # Wrap in $exit / $continue blocks
    lines.append(f'    block ;; $exit\n')
    lines.append(f'      block ;; $continue\n')

    # Emit transformed block code
    transformed = transform_extracted_block_code(block_code, config, param_offset,
                                                  exit_labels_info,
                                                  skip_local_transform=skip_local_transform)
    for tl in transformed:
        lines.append(f'        {tl.strip()}\n')

    # Close $continue — store locals and return 0
    lines.append(f'      end ;; $continue\n')
    lines.extend(generate_store_locals_helper(config, param_offset, '      '))
    lines.append(f'      i32.const 0\n')
    lines.append(f'      return\n')

    # Close $exit — store locals and return result
    lines.append(f'    end ;; $exit\n')
    lines.extend(generate_store_locals_helper(config, param_offset, '    '))
    lines.append(f'    local.get {result_local_idx}\n')
    lines.append(f'  )\n')

    return lines


def split_block_extraction_func(wat_lines, config, next_func_idx):
    """Split a function using the block extraction strategy.

    Extracts large blocks at specified depths into helper functions.
    Each helper takes a frame pointer, returns a continuation code:
      0 = normal exit (fall through)
      N = branch to Nth enclosing block/loop/if

    Returns (func_start, func_end, modified_func_lines, helper_func_lines_list,
             next_func_idx, new_type_line).
    """
    func_idx = config['func_index']
    func_start, func_end = find_func_boundaries(wat_lines, func_idx)
    func_lines = wat_lines[func_start:func_end]
    print(f'  func {func_idx} at lines {func_start + 1}-{func_end}')
    print(f'  {func_end - func_start} lines')

    _, local_offsets, new_frame_size, _ = compute_spill_layout(config)
    orig_frame_size = config['orig_frame_size']
    fp_local = config['frame_pointer_local']
    all_types = all_local_types(config)
    num_orig_locals = len(all_types)
    target_depths = config.get('extraction_depths', [config.get('extraction_depth', 4)])
    min_size = config['min_block_size']
    max_size = config.get('max_block_size', None)

    # Parse block structure
    blocks = parse_block_structure(func_lines)

    # Find extraction targets at all specified depths
    targets = find_extraction_targets(blocks, target_depths, min_size, func_lines, max_size)
    print(f'  Found {len(targets)} extraction targets:')
    for t_idx in targets:
        b = blocks[t_idx]
        size = b['end_line'] - b['start_line']
        print(f'    {b["kind"]} @{b["label"]} depth={b["depth"]} (lines {b["start_line"]}-{b["end_line"]}, {size} lines)')

    if not targets:
        print(f'  No extraction targets found, skipping')
        return func_start, func_end, func_lines, [], next_func_idx, None

    # Find or create helper function type: (param i32) → (result i32)
    helper_type_idx, new_type_line = find_or_create_type(wat_lines, ['i32'], ['i32'])

    # Generate helper functions for each target block
    param_offset = 1  # helper has 1 param: $frame
    helper_functions = []
    helper_indices = []

    for t_idx in targets:
        b = blocks[t_idx]
        block_code = func_lines[b['start_line'] + 1:b['end_line']]
        exit_labels = compute_exit_labels_for_block(blocks, t_idx, func_lines)

        helper_func_idx = next_func_idx
        helper = generate_block_helper_function(
            helper_func_idx, helper_type_idx, config,
            block_code, exit_labels, param_offset
        )
        helper_functions.append(helper)
        helper_indices.append(helper_func_idx)
        next_func_idx += 1
        print(f'    Helper func {helper_func_idx}: {len(helper)} lines')

    # Generate modified main function
    modified = list(func_lines)

    # Add a spare local (i32) for continuation code dispatch.
    # The spare local index = num_orig_locals.
    spare_local = num_orig_locals
    # Modify the locals declaration (line 1 of the function) to add i32
    locals_line = modified[1]
    if '(local' in locals_line:
        # Append i32 before the closing paren
        modified[1] = locals_line.rstrip().rstrip(')') + ' i32)\n'
    else:
        # No locals yet, add a new locals declaration
        modified.insert(1, '    (local i32)\n')

    # Replace the frame size in setup code
    for i in range(min(20, len(modified))):
        if f'i32.const {orig_frame_size}' in modified[i]:
            modified[i] = modified[i].replace(f'i32.const {orig_frame_size}', f'i32.const {new_frame_size}')

    # Process targets in reverse order (to preserve line numbers)
    for t_i in reversed(range(len(targets))):
        t_idx = targets[t_i]
        b = blocks[t_idx]
        helper_func_idx = helper_indices[t_i]

        # Compute exit labels for continuation dispatch
        exit_labels = compute_exit_labels_for_block(blocks, t_idx, func_lines)
        max_code = max(c for _, c in exit_labels)

        # Determine indent from the original block line
        block_line = func_lines[b['start_line']]
        base_indent = block_line[:len(block_line) - len(block_line.lstrip())]
        inner_indent = base_indent + '  '

        # Generate replacement code for the extracted block
        replacement = []

        # Keep the original block/if/loop opening
        replacement.append(block_line)

        # Spill locals
        replacement.append(f'{inner_indent};; === SPILL (extracted block) ===\n')
        for li in range(num_orig_locals):
            offset, t = local_offsets[li]
            replacement.append(f'{inner_indent}local.get {fp_local}\n')
            replacement.append(f'{inner_indent}local.get {li}\n')
            replacement.append(f'{inner_indent}{store_op(t)} offset={offset}\n')

        # Call helper
        replacement.append(f'{inner_indent}local.get {fp_local}\n')
        replacement.append(f'{inner_indent}call {helper_func_idx}\n')

        # Save result (continuation code) to spare local BEFORE reload
        replacement.append(f'{inner_indent}local.set {spare_local}\n')

        # Reload locals
        replacement.append(f'{inner_indent};; === RELOAD (extracted block) ===\n')
        for li in range(num_orig_locals):
            offset, t = local_offsets[li]
            replacement.append(f'{inner_indent}local.get {fp_local}\n')
            replacement.append(f'{inner_indent}{load_op(t)} offset={offset}\n')
            replacement.append(f'{inner_indent}local.set {li}\n')

        # Dispatch on continuation code using br_table.
        # From inside this block, code N maps directly to br N:
        #   code 0 → br 0 → exit this block (normal completion)
        #   code 1 → br 1 → exit parent
        #   code 2 → br 2 → exit grandparent
        #   ... etc.
        replacement.append(f'{inner_indent};; === CONTINUATION DISPATCH ===\n')
        replacement.append(f'{inner_indent}local.get {spare_local}\n')
        br_entries = ' '.join(str(i) for i in range(max_code + 1))
        replacement.append(f'{inner_indent}br_table {br_entries} {max_code}\n')

        # Close the block (the original 'end' stays)
        replacement.append(func_lines[b['end_line']])

        # Replace in modified
        modified[b['start_line']:b['end_line'] + 1] = replacement

    # Also fix frame size in epilogue
    for i in range(len(modified) - 20, len(modified)):
        if i >= 0 and f'i32.const {orig_frame_size}' in modified[i]:
            modified[i] = modified[i].replace(f'i32.const {orig_frame_size}', f'i32.const {new_frame_size}')

    print(f'  Modified func: {len(modified)} lines (was {len(func_lines)})')

    return func_start, func_end, modified, helper_functions, next_func_idx, new_type_line


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 3:
        print(f'Usage: {sys.argv[0]} <input.wasm> <output.wasm>')
        sys.exit(1)

    input_wasm = sys.argv[1]
    output_wasm = sys.argv[2]

    print(f'[1] Converting {input_wasm} to WAT...')
    with tempfile.NamedTemporaryFile(suffix='.wat', delete=False, mode='w') as f:
        wat_file = f.name
    subprocess.run(['wasm2wat', input_wasm, '-o', wat_file], check=True)

    with open(wat_file, 'r') as f:
        wat_lines = f.readlines()
    print(f'  {len(wat_lines)} lines')

    last_func_idx = find_last_func_index(wat_lines)
    print(f'  Last function index: {last_func_idx}')
    next_func_idx = last_func_idx + 1

    # Collect all transformations: (func_start, func_end, modified_lines)
    transformations = []
    all_new_functions = []
    new_type_lines = []

    for config in ALL_CONFIGS:
        func_idx = config['func_index']
        strategy = config['strategy']
        print(f'\n[*] Processing func {func_idx} ({strategy})...')

        if strategy == 'br_table':
            func_start, func_end, modified, groups, next_func_idx, new_type = \
                split_brtable_func(wat_lines, config, next_func_idx)
            transformations.append((func_start, func_end, modified))
            all_new_functions.extend(groups)
            if new_type:
                new_type_lines.append(new_type)

        elif strategy == 'block_extraction':
            func_start, func_end, modified, helpers, next_func_idx, new_type = \
                split_block_extraction_func(wat_lines, config, next_func_idx)
            transformations.append((func_start, func_end, modified))
            all_new_functions.extend(helpers)
            if new_type:
                new_type_lines.append(new_type)

    # Sort transformations by start line (descending) for safe replacement
    transformations.sort(key=lambda t: t[0], reverse=True)

    print(f'\n[*] Assembling output WAT...')

    # Insert new type lines (if any) after the last existing type
    if new_type_lines:
        # Find the last type line
        last_type_line = 0
        for i, line in enumerate(wat_lines):
            if re.match(r'\s*\(type \(;\d+;\)', line):
                last_type_line = i
        for new_type in new_type_lines:
            wat_lines.insert(last_type_line + 1, new_type)
            # Adjust all transformation start/end positions
            for j in range(len(transformations)):
                fs, fe, mod = transformations[j]
                if fs > last_type_line:
                    transformations[j] = (fs + 1, fe + 1, mod)
            last_type_line += 1

    # Apply transformations (already sorted descending by start)
    for func_start, func_end, modified in transformations:
        wat_lines[func_start:func_end] = modified

    # Append new functions before the final closing paren
    output_lines = wat_lines[:-1]
    for gf in all_new_functions:
        output_lines.extend(gf)
    output_lines.append(wat_lines[-1])

    # Write output WAT
    output_wat = output_wasm.replace('.wasm', '.wat')
    with open(output_wat, 'w') as f:
        f.writelines(output_lines)
    print(f'  Written {len(output_lines)} lines to {output_wat}')

    # Compile with wat2wasm
    print(f'\nCompiling with wat2wasm...')
    result = subprocess.run(
        ['wat2wasm', output_wat, '-o', output_wasm],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f'ERROR: wat2wasm failed:')
        errors = result.stderr.strip().split('\n')
        for e in errors[:30]:
            print(f'  {e}')
        if len(errors) > 30:
            print(f'  ... and {len(errors) - 30} more errors')
        sys.exit(1)
    else:
        print(f'  Success! Output: {output_wasm}')

    # Report sizes
    output_size = os.path.getsize(output_wasm)
    input_size = os.path.getsize(input_wasm)
    print(f'\n  Input size:  {input_size:,} bytes')
    print(f'  Output size: {output_size:,} bytes')
    print(f'  Overhead:    {output_size - input_size:,} bytes ({(output_size / input_size - 1) * 100:.1f}%)')


if __name__ == '__main__':
    main()
