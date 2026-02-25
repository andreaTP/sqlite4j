#!/usr/bin/env python3
"""
Wasm → Wasm transformation: split func[482] (SQLite VDBE main loop)
into a thin dispatcher + N group functions.

Usage: python3 split_func482.py wasm-lib/libsqlite3.wasm wasm-lib/libsqlite3_split.wasm
"""

import re
import subprocess
import sys
import os
import tempfile

# ─── Configuration ───────────────────────────────────────────────────────────

NUM_GROUPS = 5
SPILL_BASE = 1280       # offset in stack frame for locals spill area
NEW_FRAME_SIZE = 1440   # extended stack frame (was 1280)
FUNC_482_INDEX = 482
NUM_IMPORTS = 58         # number of imported functions

# Structural label → continuation code mapping
CONTINUATION_CODES = {
    8: 0,   # @8 = continue (next opcode)
    7: 6,   # @7 = re-enter if
    6: 5,   # @6 = loop continue
    5: 4,   # @5 = exit dispatch
    4: 3,   # @4 = early exit
    3: 7,   # @3 = main body exit
    2: 2,   # @2 = inner loop
    1: 1,   # @1 = outer loop
}

# ─── Parsing ─────────────────────────────────────────────────────────────────

def parse_func482(wat_lines, func_start, func_end):
    """Parse func 482 and extract handler structure."""
    func_lines = wat_lines[func_start:func_end]

    # Find the main br_table (the one with >200 chars)
    br_table_line = None
    for i, line in enumerate(func_lines):
        stripped = line.strip()
        if stripped.startswith('br_table') and len(stripped) > 200:
            br_table_line = i
            break

    if br_table_line is None:
        raise ValueError("Could not find main br_table in func 482")

    # Parse br_table entries
    br_line = func_lines[br_table_line].strip()
    entries = re.findall(r'(\d+)\s*\(;@\d+;\)', br_line)
    br_table_entries = [int(e) for e in entries]

    # Build label stack at br_table position
    label_stack = []
    for i in range(len(func_lines)):
        stripped = func_lines[i].strip()
        if i < 2:  # skip func header and locals
            continue

        label_match = re.search(r';; label = @(\d+)', stripped)

        if stripped.startswith(('block', 'loop', 'if')):
            kind = stripped.split()[0].split('(')[0]
            label = int(label_match.group(1)) if label_match else -1
            label_stack.append((kind, label, i))
        elif stripped == 'end' or (stripped.startswith('end') and not stripped.startswith('end)')):
            if label_stack:
                label_stack.pop()

        if i == br_table_line:
            break

    depth_at_brtable = len(label_stack)

    # Find handler boundaries
    handlers = []
    handler_start_line = br_table_line + 1
    d = depth_at_brtable

    for i in range(br_table_line + 1, len(func_lines)):
        stripped = func_lines[i].strip()
        if stripped.startswith(('block', 'loop', 'if')):
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

    # Separate real opcode handlers from structural code
    # The dispatch blocks are @9 through @285 = 277 blocks.
    # There are 278 handler regions: 277 between dispatch block ends + 1 between
    # the last dispatch block end and @8's end (this last one handles the br_table
    # case that targets @285, the outermost dispatch block).
    num_dispatch_blocks = len(label_stack) - 8  # = 277 dispatch blocks
    num_opcode_handlers = num_dispatch_blocks + 1  # = 278 (includes post-dispatch handler)
    opcode_handlers = handlers[:num_opcode_handlers]
    structural_code = handlers[num_opcode_handlers:]

    print(f"  br_table at line {br_table_line + 1} (within func)")
    print(f"  {len(br_table_entries)} br_table entries")
    print(f"  {len(opcode_handlers)} opcode handlers")
    print(f"  {len(structural_code)} structural code regions")
    print(f"  Depth at br_table: {len(label_stack)}")

    return {
        'func_lines': func_lines,
        'br_table_line': br_table_line,
        'br_table_entries': br_table_entries,
        'opcode_handlers': opcode_handlers,
        'structural_code': structural_code,
        'label_stack': label_stack,
        'depth_at_brtable': len(label_stack),
    }


# ─── Handler code transformation ────────────────────────────────────────────

def transform_handler_code(handler_code, handler_orig_index, group_start, group_end, adj):
    """
    Transform a handler's code for use in a group function.

    Changes:
    1. local.get/set/tee N → local.get/set/tee N+2 (offset by 2 params)
    2. br/br_if to external targets → adjusted depths
    3. Internal br/br_if (within handler's own blocks) → unchanged depths

    Key insight: we track internal_depth (blocks opened within the handler) to
    distinguish handler-internal branches from external branches. Labels like @8
    can be REUSED for handler-internal blocks, so we can't rely on labels alone.
    """
    result_local_idx = 2 + 34  # offset by 2 params + 34 original locals = local index 36
    H = handler_orig_index
    H_local = H - group_start
    G_size = group_end - group_start

    # Depth thresholds (from handler H's base level, i.e., internal_depth=0):
    # - 0 to (G_size-2-H_local): within-group dispatch blocks
    # - (G_size-1-H_local): $continue (was @8)
    # - (G_size-H_local): $exit
    # In original:
    # - 0 to (275-H): dispatch blocks
    # - (276-H): @8
    # - (277-H): @7
    # - ...
    orig_depth_to_8 = 277 - H  # from handler base to @8 in original

    internal_depth = 0  # tracks nesting within the handler's own code

    transformed = []
    for line in handler_code:
        stripped = line.strip()

        # Track internal nesting
        if stripped.startswith(('block', 'loop', 'if')) and not stripped.startswith(('block)', 'loop)', 'if)')):
            internal_depth += 1
        # Note: 'end' reduces internal_depth, but we process it AFTER handling
        # br instructions on the same line (which won't happen — end doesn't have br)

        # Transform local.get/set/tee
        local_match = re.match(r'^(\s*)(local\.(get|set|tee))\s+(\d+)(.*)', line.rstrip())
        if local_match:
            indent = local_match.group(1)
            op = local_match.group(2)
            idx = int(local_match.group(4))
            rest = local_match.group(5)
            new_idx = idx + 2  # offset for $frame and $handler_idx params
            transformed.append(f"{indent}{op} {new_idx}{rest}\n")
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

            # Check if this targets an internal block
            if old_depth < internal_depth:
                # Internal target — no depth adjustment needed, just emit as-is
                transformed.append(line)
            else:
                # External target — need to adjust depth
                effective_external = old_depth - internal_depth

                if effective_external < G_size - 1 - H_local:
                    # Within-group dispatch block — depth unchanged
                    transformed.append(line)
                elif effective_external == orig_depth_to_8:
                    # Was @8 (continue) → adjust to $continue
                    new_depth = internal_depth + (G_size - 1 - H_local)
                    transformed.append(f"{indent}{op} {new_depth} ;; @8→$continue\n")
                elif effective_external > orig_depth_to_8:
                    # Structural target (@7 through @1) → set result + br to $exit
                    structural_offset = effective_external - orig_depth_to_8
                    # structural_offset 1=@7, 2=@6, 3=@5, 4=@4, 5=@3, 6=@2, 7=@1
                    structural_label = 8 - structural_offset
                    cont_code = CONTINUATION_CODES.get(structural_label, structural_label)
                    new_depth_exit = internal_depth + (G_size - H_local)

                    if op == 'br':
                        transformed.append(f"{indent}i32.const {cont_code}\n")
                        transformed.append(f"{indent}local.set {result_local_idx}\n")
                        transformed.append(f"{indent}br {new_depth_exit} ;; @{structural_label}→$exit\n")
                    else:  # br_if
                        transformed.append(f"{indent}if\n")
                        transformed.append(f"{indent}  i32.const {cont_code}\n")
                        transformed.append(f"{indent}  local.set {result_local_idx}\n")
                        transformed.append(f"{indent}  br {new_depth_exit + 1} ;; @{structural_label}→$exit (+1 for if)\n")
                        transformed.append(f"{indent}end\n")
                else:
                    # Out-of-group dispatch block → cross-group jump
                    # The target handler index: from handler H, effective_external K
                    # targets the block at position K from the base, which wraps handler H+K+1
                    target_handler_idx = H + effective_external + 1
                    cont_code = 100 + target_handler_idx
                    new_depth_exit = internal_depth + (G_size - H_local)

                    if op == 'br':
                        transformed.append(f"{indent}i32.const {cont_code}\n")
                        transformed.append(f"{indent}local.set {result_local_idx}\n")
                        transformed.append(f"{indent}br {new_depth_exit} ;; cross-group handler {target_handler_idx}→$exit\n")
                    else:
                        transformed.append(f"{indent}if\n")
                        transformed.append(f"{indent}  i32.const {cont_code}\n")
                        transformed.append(f"{indent}  local.set {result_local_idx}\n")
                        transformed.append(f"{indent}  br {new_depth_exit + 1} ;; cross-group handler {target_handler_idx}→$exit (+1 for if)\n")
                        transformed.append(f"{indent}end\n")

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
                        # Internal target — unchanged
                        new_entries.append(depth_str)
                    else:
                        effective_external = old_d - internal_depth
                        if effective_external < G_size - 1 - H_local:
                            # Within-group — unchanged
                            new_entries.append(depth_str)
                        elif effective_external == orig_depth_to_8:
                            # Was @8 → $continue
                            new_d = internal_depth + (G_size - 1 - H_local)
                            new_entries.append(str(new_d))
                        elif effective_external > orig_depth_to_8:
                            # Structural → $exit
                            new_d = internal_depth + (G_size - H_local)
                            new_entries.append(str(new_d))
                        else:
                            # Out-of-group dispatch → $exit
                            new_d = internal_depth + (G_size - H_local)
                            new_entries.append(str(new_d))

                indent = line[:len(line) - len(line.lstrip())]
                transformed.append(f"{indent}br_table {' '.join(new_entries)}\n")
            else:
                # br_table without label comments — just pass through
                transformed.append(line)

            if stripped.startswith('end'):
                internal_depth -= 1
            continue

        # Track 'end' for internal depth
        if stripped == 'end' or (stripped.startswith('end') and not stripped.startswith('end)')):
            internal_depth -= 1

        # No transformation needed for other instructions
        transformed.append(line)

    return transformed


# ─── Generate group function ────────────────────────────────────────────────

def generate_group_function(group_id, group_start, group_end, handlers, func_type_idx, last_func_idx_base):
    """Generate a group handler function."""
    g_size = group_end - group_start
    adj = 277 - g_size  # depth adjustment for this group

    # Local types: 2 params (i32) + 32 i32 + 2 i64 + 1 i32 (result) = 37 total
    # Original func 482 has 34 locals: 2 i32 params + 30 i32 declared + 2 i64 declared
    # Group local N+2 maps to original local N, so we need 32 i32 + 2 i64 to cover all.
    lines = []
    func_idx = last_func_idx_base + group_id
    lines.append(f"  (func (;{func_idx};) (type {func_type_idx}) (param i32 i32) (result i32)\n")

    # Declare locals: 32 i32 (orig locals 0-31) + 2 i64 (orig locals 32-33) + 1 i32 ($result)
    lines.append(f"    (local i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i64 i64 i32)\n")

    # Load all 34 original locals from spill area
    # Original locals 0-31 are i32 (at offset SPILL_BASE + N*4)
    # Original locals 32-33 are i64 (at offset SPILL_BASE + 128 + (N-32)*8)
    for i in range(32):
        offset = SPILL_BASE + i * 4
        lines.append(f"    local.get 0\n")  # $frame
        lines.append(f"    i32.load offset={offset}\n")
        lines.append(f"    local.set {i + 2}\n")  # offset by 2 params
    for i in range(2):
        offset = SPILL_BASE + 128 + i * 8
        lines.append(f"    local.get 0\n")
        lines.append(f"    i64.load offset={offset}\n")
        lines.append(f"    local.set {32 + i + 2}\n")

    # Dispatch structure:
    # block $exit
    #   block $continue
    #     block $d_{g_size-1}
    #       ...
    #       block $d_0
    #         local.get 1 ;; $handler_idx
    #         i32.const group_start
    #         i32.sub
    #         br_table 0 1 2 ... (g_size-1) (g_size)
    #       end $d_0
    #       ;; handler group_start code
    #     end $d_1
    #     ;; handler group_start+1 code
    #     ...
    #   end $continue
    #   ;; store locals, return 0
    # end $exit
    # ;; store locals, return result

    # Open $exit and $continue blocks
    lines.append(f"    block ;; $exit\n")
    lines.append(f"      block ;; $continue\n")

    # Open dispatch blocks (from outermost to innermost)
    for i in range(g_size - 1, -1, -1):
        lines.append(f"        block ;; $d_{i}\n")

    # br_table dispatch
    lines.append(f"          local.get 1\n")  # $handler_idx
    if group_start > 0:
        lines.append(f"          i32.const {group_start}\n")
        lines.append(f"          i32.sub\n")
    br_entries = ' '.join(str(i) for i in range(g_size))
    # Default goes to $continue (depth g_size)
    lines.append(f"          br_table {br_entries} {g_size}\n")

    # Close dispatch blocks and emit handler code
    for i in range(g_size):
        handler = handlers[group_start + i]
        lines.append(f"        end ;; $d_{i} — handler {group_start + i}\n")

        # Transform and emit handler code
        transformed = transform_handler_code(
            handler['code'], group_start + i,
            group_start, group_end, adj
        )
        for tl in transformed:
            # Reindent to match the nesting
            lines.append(f"        {tl.strip()}\n")

    # Close $continue block — store locals and return 0
    lines.append(f"      end ;; $continue\n")
    lines.extend(generate_store_locals("      "))
    lines.append(f"      i32.const 0\n")
    lines.append(f"      return\n")

    # Close $exit block — store locals and return result
    lines.append(f"    end ;; $exit\n")
    lines.extend(generate_store_locals("    "))
    lines.append(f"    local.get 36\n")  # $result local
    lines.append(f"  )\n")

    return lines


def generate_store_locals(indent):
    """Generate code to store all locals back to spill area (in group functions).
    In group functions: local 0 = $frame, locals 2..35 = original locals 0..33."""
    lines = []
    for i in range(32):
        offset = SPILL_BASE + i * 4
        lines.append(f"{indent}local.get 0\n")  # $frame
        lines.append(f"{indent}local.get {i + 2}\n")  # original local i
        lines.append(f"{indent}i32.store offset={offset}\n")
    for i in range(2):
        offset = SPILL_BASE + 128 + i * 8
        lines.append(f"{indent}local.get 0\n")  # $frame
        lines.append(f"{indent}local.get {32 + i + 2}\n")  # original local 32+i
        lines.append(f"{indent}i64.store offset={offset}\n")
    return lines


def generate_store_locals_main(indent):
    """Generate code to store all locals to spill area (in main func 482)."""
    lines = []
    for i in range(32):
        offset = SPILL_BASE + i * 4
        lines.append(f"{indent}local.get 14\n")  # frame pointer is local 14 in original
        lines.append(f"{indent}local.get {i}\n")
        lines.append(f"{indent}i32.store offset={offset}\n")
    for i in range(2):
        offset = SPILL_BASE + 128 + i * 8
        lines.append(f"{indent}local.get 14\n")
        lines.append(f"{indent}local.get {32 + i}\n")
        lines.append(f"{indent}i64.store offset={offset}\n")
    return lines


def generate_reload_locals_main(indent):
    """Generate code to reload all locals from spill area (in main func 482)."""
    lines = []
    for i in range(32):
        offset = SPILL_BASE + i * 4
        lines.append(f"{indent}local.get 14\n")
        lines.append(f"{indent}i32.load offset={offset}\n")
        lines.append(f"{indent}local.set {i}\n")
    for i in range(2):
        offset = SPILL_BASE + 128 + i * 8
        lines.append(f"{indent}local.get 14\n")
        lines.append(f"{indent}i64.load offset={offset}\n")
        lines.append(f"{indent}local.set {32 + i}\n")
    return lines


# ─── Generate modified func 482 ─────────────────────────────────────────────

def generate_modified_func482(parsed, group_boundaries, new_func_indices):
    """
    Generate the modified func 482 with handler stubs.

    Strategy: replace block @8's content (dispatch blocks + handler code) with:
    1. Spill locals
    2. Use original br_table to set handler_idx, then br to @8
    3. After @8: dispatch to group, call group function, reload, handle continuation
    """
    func_lines = parsed['func_lines']
    br_table_line = parsed['br_table_line']
    opcode_handlers = parsed['opcode_handlers']
    structural_code = parsed['structural_code']
    br_table_entries = parsed['br_table_entries']

    # We need to find key line numbers within func 482:
    # - Start of block @8 (line 416 in func-local coords, but let's find it precisely)
    # - End of block @8 (after all opcode handlers)
    # - The line of the br_table

    # Find block @8 start
    label_stack = []
    block_8_start = None
    block_8_label_idx = None
    for i in range(len(func_lines)):
        stripped = func_lines[i].strip()
        if i < 2:
            continue
        label_match = re.search(r';; label = @(\d+)', stripped)
        if stripped.startswith(('block', 'loop', 'if')):
            kind = stripped.split()[0].split('(')[0]
            label = int(label_match.group(1)) if label_match else -1
            label_stack.append((kind, label, i))
        elif stripped == 'end' or (stripped.startswith('end') and not stripped.startswith('end)')):
            if label_stack:
                label_stack.pop()
        if i == br_table_line:
            break

    # @8 is at stack position 7
    block_8_info = None
    for idx, (kind, label, line) in enumerate(label_stack):
        if label == 8 and idx == 7:
            block_8_info = (kind, label, line)
            break

    if block_8_info is None:
        raise ValueError("Could not find block @8")

    block_8_start_line = block_8_info[2]

    # Find where block @8 ends — it's after all opcode handlers
    if opcode_handlers:
        last_handler = opcode_handlers[-1]
        block_8_end_line = last_handler['end'] + 1  # the 'end' that closes @8
    else:
        raise ValueError("No opcode handlers found")

    # Find where the structural code regions end
    if structural_code:
        last_structural = structural_code[-1]
        structural_end_line = last_structural['end'] + 1
    else:
        structural_end_line = block_8_end_line

    print(f"  Block @8 starts at line {block_8_start_line + 1}")
    print(f"  Block @8 ends at line {block_8_end_line + 1}")

    # We need an extra local for handler_idx
    # Original has 34 locals (indices 0-33). We'll add local 34 (i32) for handler_idx.

    # Build the new func 482
    result = []

    # 1. Copy everything up to the func header, modify locals declaration
    # Line 0: func declaration
    result.append(func_lines[0])
    # Line 1: locals — add one more i32
    old_locals = func_lines[1].strip()
    # Original: (local i32 i32 ... i32 i64 i64)
    # New: add one more i32 at the end
    result.append("    (local i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i64 i64 i32)\n")

    # 2. Copy setup code up to the block @8 start, changing frame size from 1280 to 1440
    for i in range(2, block_8_start_line):
        line = func_lines[i]
        # Change stack frame allocation: i32.const 1280 → i32.const 1440
        if 'i32.const 1280' in line and i < 10:
            line = line.replace('i32.const 1280', f'i32.const {NEW_FRAME_SIZE}')
        result.append(line)

    # 3. Replace block @8 and its contents

    # Keep the "block @8" opening but change what's inside
    # The original block @8 opens dispatch blocks and contains the br_table + handlers
    # We replace with: spill → stub dispatch → br @8

    # First, emit the spill code BEFORE block @8
    result.append("    ;; === SPILL LOCALS ===\n")
    result.extend(generate_store_locals_main("    "))

    # Initialize handler_idx to -1 (sentinel for "no handler").
    # br_table entries with depth 277 target @8 directly, bypassing all stubs.
    # The sentinel ensures the group function's default case handles this (returns 0 = continue).
    handler_idx_local = 34  # the new local we added
    result.append(f"    i32.const -1\n")
    result.append(f"    local.set {handler_idx_local}\n")

    # Now emit block @8 with stub dispatch
    result.append(func_lines[block_8_start_line])  # block ;; label = @8

    # Emit the dispatch blocks (same structure as original, for the br_table)
    for i in range(block_8_start_line + 1, br_table_line):
        result.append(func_lines[i])

    # Emit the br_table (unchanged — it still dispatches to the right blocks)
    result.append(func_lines[br_table_line])

    # Now emit handler stubs: one per dispatch block (277 stubs).
    # Note: num_handlers includes the post-dispatch handler (278 total), but only
    # 277 stubs need 'end' statements (one per dispatch block). The 278th handler
    # (between last dispatch block and @8) gets a stub without a preceding 'end'.
    num_handlers = len(opcode_handlers)  # 278
    num_dispatch_stubs = num_handlers - 1  # 277 (one per dispatch block)

    for h_idx in range(num_dispatch_stubs):
        # Emit the 'end' that closes this dispatch block
        result.append("                  end\n")

        # Stub: set handler index and jump to @8
        # At stub h_idx's position, @8 is at depth (num_dispatch_stubs - 1 - h_idx)
        depth_to_8 = num_dispatch_stubs - 1 - h_idx
        result.append(f"                  i32.const {h_idx + 1}\n")
        result.append(f"                  local.set {handler_idx_local}\n")
        result.append(f"                  br {depth_to_8}\n")

    # Stub for handler 277 (the post-dispatch handler, between last dispatch block and @8).
    # No 'end' needed — we're between the last dispatch block's end and @8's end.
    # This handler's code (structural_code[0] in original) is in the last group function.
    result.append(f"                  i32.const {num_dispatch_stubs}\n")  # handler_idx = 277
    result.append(f"                  local.set {handler_idx_local}\n")

    # Close block @8
    result.append("                end ;; @8\n")

    # FIX: local 28 is set by `local.tee 28` inside the dispatch blocks (right
    # before the br_table), AFTER the spill.  We must update the spill area so
    # the group function (and subsequent reload) sees the correct opcode index.
    result.append(f"    local.get 14\n")  # frame pointer
    result.append(f"    local.get 28\n")  # opcode index (set by local.tee 28)
    result.append(f"    i32.store offset={SPILL_BASE + 28 * 4}\n")

    # 4. After block @8: dispatch to group function and handle result
    result.append("    ;; === DISPATCH TO GROUP ===\n")

    # Wrap group dispatch in a loop to handle cross-group jumps.
    # If a handler returns code >= 100, it means "re-dispatch to handler (code-100)".
    # The previous group function already stored locals back to the spill area,
    # so we just need to call the correct group function with the new handler_idx.
    result.append(f"    loop ;; $redispatch\n")

    # Build group dispatch using if-else chain.
    # Each if has (result i32) — both true and false branches must produce an i32.
    num_groups = len(group_boundaries)
    for g_idx in range(num_groups):
        g_start, g_end = group_boundaries[g_idx]
        func_idx = new_func_indices[g_idx]

        if g_idx < num_groups - 1:
            next_g_start = group_boundaries[g_idx + 1][0]
            result.append(f"    local.get {handler_idx_local}\n")
            result.append(f"    i32.const {next_g_start}\n")
            result.append(f"    i32.lt_u\n")
            result.append(f"    if (result i32)\n")
            result.append(f"      local.get 14\n")  # frame pointer
            result.append(f"      local.get {handler_idx_local}\n")
            result.append(f"      call {func_idx}\n")
            result.append(f"    else\n")
        else:
            # Last group — no condition needed (this is the else branch of the previous if)
            result.append(f"      local.get 14\n")
            result.append(f"      local.get {handler_idx_local}\n")
            result.append(f"      call {func_idx}\n")

    # Close the if-else chain
    for g_idx in range(num_groups - 1):
        result.append(f"    end\n")

    # Result (continuation code) is now on the stack. Store it.
    result.append(f"    local.set {handler_idx_local}\n")  # reuse handler_idx for result

    # Check for cross-group jump: if result >= 100, re-dispatch
    result.append(f"    local.get {handler_idx_local}\n")
    result.append(f"    i32.const 100\n")
    result.append(f"    i32.ge_u\n")
    result.append(f"    if\n")
    result.append(f"      local.get {handler_idx_local}\n")
    result.append(f"      i32.const 100\n")
    result.append(f"      i32.sub\n")
    result.append(f"      local.set {handler_idx_local}\n")
    result.append(f"      br 1 ;; restart $redispatch loop\n")
    result.append(f"    end\n")
    result.append(f"    end ;; $redispatch loop\n")

    # 5. Reload locals from spill area
    result.append("    ;; === RELOAD LOCALS ===\n")
    result.extend(generate_reload_locals_main("    "))

    # 6. Handle continuation code
    result.append("    ;; === HANDLE CONTINUATION ===\n")
    # Code 0 = continue (fall through — @8 has ended, structural code continues)
    # Code 1-6 = branch to structural labels @1-@7
    #
    # After block @8 ends, we're inside (from outermost to innermost):
    #   @1 (loop), @2 (loop), @3 (block), @4 (block), @5 (block), @6 (loop), @7 (if)
    # Our continuation "if" adds one more level.
    # Inside the "if", we add 6 blocks for the br_table dispatch.
    #
    # Depth calculation from each br position (after the corresponding block closes):
    #   After block 0 ends (inside block 1): @1=12, @2=11, @3=10, @4=9, @5=8, @6=7, @7=6
    #   After block 1 ends (inside block 2): @1=11, @2=10, @3=9, @4=8, @5=7, @6=6, @7=5
    #   After block 2 ends (inside block 3): @1=10, @2=9, @3=8, @4=7, @5=6, @6=5, @7=4
    #   After block 3 ends (inside block 4): @1=9, @2=8, @3=7, @4=6, @5=5, @6=4, @7=3
    #   After block 4 ends (inside block 5): @1=8, @2=7, @3=6, @4=5, @5=4, @6=3, @7=2
    #   After block 5 ends (inside cont if): @1=7, @2=6, @3=5, @4=4, @5=3, @6=2, @7=1

    result.append(f"    local.get {handler_idx_local}\n")
    result.append(f"    if\n")
    # index 0 (code 1) → @1,  index 1 (code 2) → @2
    # index 2 (code 3) → @4,  index 3 (code 4) → @5
    # index 4 (code 5) → @6,  index 5 (code 6) → @7
    result.append(f"      block\n")      # block 5
    result.append(f"        block\n")    # block 4
    result.append(f"          block\n")  # block 3
    result.append(f"            block\n")    # block 2
    result.append(f"              block\n")  # block 1
    result.append(f"                block\n")  # block 0 (innermost)
    # Compute br_table index INSIDE the innermost block
    # (blocks start with empty operand stack in Wasm)
    result.append(f"                  local.get {handler_idx_local}\n")
    result.append(f"                  i32.const 1\n")
    result.append(f"                  i32.sub\n")
    result.append(f"                  br_table 0 1 2 3 4 5 0\n")
    result.append(f"                end\n")  # end block 0
    result.append(f"                br 12 ;; code 1 → @1 (outer loop)\n")
    result.append(f"              end\n")  # end block 1
    result.append(f"              br 10 ;; code 2 → @2 (inner loop)\n")
    result.append(f"            end\n")  # end block 2
    result.append(f"            br 7 ;; code 3 → @4 (early exit)\n")
    result.append(f"          end\n")  # end block 3
    result.append(f"          br 5 ;; code 4 → @5 (dispatch wrapper)\n")
    result.append(f"        end\n")  # end block 4
    result.append(f"        br 3 ;; code 5 → @6 (fetch loop)\n")
    result.append(f"      end\n")  # end block 5
    result.append(f"      br 1 ;; code 6 → @7 (valid opcode check)\n")
    result.append(f"    end\n")  # close continuation if

    # 7. Copy the remaining structural code (after block @8 through end of function)
    # This is the code that was AFTER block @8 in the original — the structural
    # code regions and the epilogue.
    # We need to find where block @8's end was and copy everything after it.

    # The structural code regions are handlers[277:] in our analysis.
    # But actually, the code after block @8's end is simply the original func_lines
    # from the line after block @8's end to the end of the function.

    # Find block @8 end line more precisely
    # block_8_end_line was set earlier as last_handler['end'] + 1
    # But actually, all the 'end' statements for the dispatch blocks AND block @8
    # are part of the handler boundaries. The 'end' for @8 itself is at
    # handlers[276]['end'] + 1... no. Let me think again.

    # The handlers were parsed by tracking depth reduction below br_table depth.
    # Handler 0 is between the first 'end' after br_table and the second 'end'.
    # Handler 276 is the last opcode handler.
    # After handler 276's end, the next 'end' closes @8.
    # Then subsequent 'end's close @7, @6, @5, @4, @3, @2, @1.

    # The structural_code list contains these post-dispatch code regions.
    # structural_code[0] = code between end @8 and end @7
    # structural_code[1] = code between end @7 and end @6
    # etc.

    # 7. Copy the remaining structural code (after block @8 through end of function)
    # opcode_handlers[-1] is handler 277 (the post-dispatch handler).
    # Its 'end' is the line of 'end @8' in the original.
    # We already emitted 'end ;; @8' in the stubs, so skip past it.
    remaining_start = opcode_handlers[-1]['end'] + 1
    # This starts at the code between end @8 and end @7 (structural_code[0]).

    for i in range(remaining_start, len(func_lines)):
        line = func_lines[i]
        # Change frame restoration: i32.const 1280 → i32.const NEW_FRAME_SIZE
        if 'i32.const 1280' in line:
            line = line.replace('i32.const 1280', f'i32.const {NEW_FRAME_SIZE}')
        result.append(line)

    return result


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.wasm> <output.wasm>")
        sys.exit(1)

    input_wasm = sys.argv[1]
    output_wasm = sys.argv[2]

    print(f"[1/6] Converting {input_wasm} to WAT...")
    with tempfile.NamedTemporaryFile(suffix='.wat', delete=False, mode='w') as f:
        wat_file = f.name
    subprocess.run(['wasm2wat', input_wasm, '-o', wat_file], check=True)

    with open(wat_file, 'r') as f:
        wat_lines = f.readlines()
    print(f"  {len(wat_lines)} lines")

    # Find func 482 boundaries
    func_start = None
    func_end = None
    for i, line in enumerate(wat_lines):
        if line.strip().startswith(f'(func (;{FUNC_482_INDEX};)'):
            func_start = i
        elif func_start is not None and line.strip().startswith(f'(func (;{FUNC_482_INDEX + 1};)'):
            func_end = i
            break

    if func_start is None or func_end is None:
        raise ValueError(f"Could not find func {FUNC_482_INDEX}")

    print(f"  func {FUNC_482_INDEX} at lines {func_start+1}-{func_end}")

    print(f"\n[2/6] Parsing func {FUNC_482_INDEX}...")
    parsed = parse_func482(wat_lines, func_start, func_end)

    num_handlers = len(parsed['opcode_handlers'])
    print(f"\n[3/6] Computing group boundaries...")

    # Compute group boundaries — roughly equal-sized groups
    # Constraint: handlers 22, 275, 276 should ideally be in the same group
    # For simplicity, we'll handle cross-group jumps via return codes
    handlers_per_group = num_handlers // NUM_GROUPS
    group_boundaries = []
    for g in range(NUM_GROUPS):
        g_start = g * handlers_per_group
        if g == NUM_GROUPS - 1:
            g_end = num_handlers
        else:
            g_end = (g + 1) * handlers_per_group
        group_boundaries.append((g_start, g_end))
        print(f"  Group {g}: handlers [{g_start}, {g_end}) = {g_end - g_start} handlers")

    # Find the type index for (i32, i32) -> i32 (same as func 482's type)
    # From the WAT, func 482 uses type 0
    group_func_type = 0

    # New function indices: they go at the end of the function section
    # Total existing functions: 2649 (from wasm-objdump) = imports(58) + module(2591)
    total_existing_funcs = 2649 + NUM_IMPORTS  # Wait, the 2649 includes both imports and module funcs
    # Actually, wasm-objdump said "Function ... count: 2649" which is module functions only.
    # Total function indices = NUM_IMPORTS + 2649 = 58 + 2649 = 2707
    # But actually, the function indices in the WAT are 0-based including imports.
    # func[0] through func[57] are imports, func[58] through func[2706] are module funcs.
    # Wait, the wasm-objdump showed "func[58] <__wasilibc_maybe_reinitialize_environ_eagerly>"
    # So the first module function is indeed at index 58.
    # And with 2649 module functions, the last is at index 58+2649-1 = 2706.
    # New functions start at index 2707.

    # Let me count functions in the WAT more carefully
    num_module_funcs = 0
    for line in wat_lines:
        if re.match(r'\s*\(func \(;\d+;\)', line):
            num_module_funcs += 1

    print(f"  Module functions in WAT: {num_module_funcs}")

    # Find the last function index
    last_func_idx = None
    for line in reversed(wat_lines):
        m = re.match(r'\s*\(func \(;(\d+);\)', line)
        if m:
            last_func_idx = int(m.group(1))
            break

    print(f"  Last function index: {last_func_idx}")

    new_func_indices = []
    for g in range(NUM_GROUPS):
        new_func_indices.append(last_func_idx + 1 + g)
    print(f"  New function indices: {new_func_indices}")

    print(f"\n[4/6] Generating group functions...")
    group_functions = []
    for g in range(NUM_GROUPS):
        g_start, g_end = group_boundaries[g]
        gf = generate_group_function(
            g, g_start, g_end,
            parsed['opcode_handlers'],
            group_func_type,
            last_func_idx + 1
        )
        group_functions.append(gf)
        handler_lines_total = sum(len(parsed['opcode_handlers'][h]['code']) for h in range(g_start, g_end))
        print(f"  Group {g}: {len(gf)} WAT lines (from {handler_lines_total} original handler lines)")

    print(f"\n[5/6] Generating modified func {FUNC_482_INDEX}...")
    modified_func = generate_modified_func482(parsed, group_boundaries, new_func_indices)
    print(f"  Modified func: {len(modified_func)} lines (was {func_end - func_start})")

    print(f"\n[6/6] Assembling output WAT...")
    # Build the output WAT:
    # 1. Everything before func 482
    # 2. Modified func 482
    # 3. Everything between func 482 end and the closing of the module
    # 4. Group functions (before the final closing paren)

    output_lines = []

    # Copy everything before func 482
    output_lines.extend(wat_lines[:func_start])

    # Insert modified func 482
    output_lines.extend(modified_func)

    # Copy everything after func 482 up to (but not including) the last line
    # The last line of the WAT should be the closing paren of the module
    output_lines.extend(wat_lines[func_end:-1])

    # Insert group functions before the final closing paren
    for g in range(NUM_GROUPS):
        output_lines.extend(group_functions[g])

    # Add the final closing paren
    output_lines.append(wat_lines[-1])

    # Write output WAT
    output_wat = output_wasm.replace('.wasm', '.wat')
    with open(output_wat, 'w') as f:
        f.writelines(output_lines)
    print(f"  Written {len(output_lines)} lines to {output_wat}")

    # Compile with wat2wasm
    print(f"\nCompiling with wat2wasm...")
    result = subprocess.run(
        ['wat2wasm', output_wat, '-o', output_wasm],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"ERROR: wat2wasm failed:")
        # Show first few errors
        errors = result.stderr.strip().split('\n')
        for e in errors[:20]:
            print(f"  {e}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more errors")
        sys.exit(1)
    else:
        print(f"  Success! Output: {output_wasm}")

    # Verify
    output_size = os.path.getsize(output_wasm)
    input_size = os.path.getsize(input_wasm)
    print(f"\n  Input size:  {input_size:,} bytes")
    print(f"  Output size: {output_size:,} bytes")
    print(f"  Overhead:    {output_size - input_size:,} bytes ({(output_size/input_size - 1)*100:.1f}%)")


if __name__ == '__main__':
    main()
