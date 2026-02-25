# Method Splitting Plan for sqlite4j — func[482]

## Target Function Profile

| Property | Value |
|---|---|
| Function index | 482 |
| Wasm binary size | 24,056 bytes |
| WAT lines | 11,106 |
| Signature | `(i32, i32) -> i32` |
| Locals | 2 params + 30 i32 + 2 i64 = **34 total** |
| Blocks | 412 |
| Loops | 29 |
| Ifs | 155 |
| br | 408 |
| br_if | 247 |
| br_table | 5 |
| Calls | 600 |

## Wasm Function Size Landscape (top 10)

| Rank | func index | Size (bytes) | Name |
|---|---|---|---|
| 1 | 180 | 31,375 | (unnamed) |
| 2 | **482** | **24,056** | **(target)** |
| 3 | 1194 | 20,835 | (unnamed) |
| 4 | 1260 | 14,847 | (unnamed) |
| 5 | 1146 | 12,121 | (unnamed) |
| 6 | 921 | 6,688 | (unnamed) |
| 7 | 1177 | 6,556 | (unnamed) |
| 8 | 103 | 6,251 | sqlite3_str_vappendf |
| 9 | 1435 | 6,066 | (unnamed) |
| 10 | 1345 | 5,861 | (unnamed) |

## Full Control Flow Structure (analyzed)

The function is SQLite's **VDBE (Virtual Database Engine)** main loop — a giant
opcode dispatch interpreter.

```
func (;482;) (param i32 i32) (result i32)
  locals: 30×i32, 2×i64

  [lines 1-54]   SETUP: stack frame (1280 bytes), load fields, init pointers
  loop @1 (line 55)                      ← OUTER LOOP
    loop @2 (line 58)                    ← INNER LOOP
      block @3 (line 59)                 ← MAIN BODY
        block @4 (line 60)               ← EARLY EXIT BLOCK
          [lines 60-277]                   early opcode handling, error checks
          block @5 (line 278)            ← DISPATCH WRAPPER
            loop @6 (line 279)           ← OPCODE FETCH LOOP
              [lines 280-362]              opcode decode, hash table lookup
              if @7 (line 363)           ← VALID OPCODE CHECK
                [lines 364-415]            hash result processing, setup
                block @8 (line 416)      ← HANDLER BLOCK (276 handlers exit here)
                  blocks @9..@285          277 dispatch blocks (nested)
                  br_table (line 698)      MAIN DISPATCH (opcode → handler)
                  [lines 699-10867]        277 OPCODE HANDLERS
                end @8                     ← handlers br here = "next opcode"
              end @7 (if)
            end @6 (loop → restart fetch)
          end @5
          [lines ~10900-11040]             post-dispatch cleanup, error handling
        end @4
        [lines ~11041-11080]               epilogue: restore state
      end @3
      [lines ~11081-11095]                 advance instruction pointer, br @2
    end @2
  end @1
  [line 11106]                             return local.get 8
```

## Label Stack at br_table (depth = 285)

```
Stack  Label   Type    Line   Purpose
[0]    @1      loop    55     Outer loop (re-enter with local 21→3 transfer)
[1]    @2      loop    58     Inner loop (continue execution)
[2]    @3      block   59     Main body
[3]    @4      block   60     Early exit
[4]    @5      block   278    Dispatch wrapper
[5]    @6      loop    279    Opcode fetch loop
[6]    @7      if      363    Valid opcode check
[7]    @8      block   416    Handler block (common exit for 276/277 handlers)
[8-284] @9-@285  block  417-693  277 dispatch blocks for opcode handlers
```

## Handler Analysis

- **277 real opcode handlers** (between dispatch blocks @9-@285)
- **8 structural code regions** after the dispatch blocks (not opcode handlers)
- Handler sizes: 108 have <10 lines, 136 have 10-50, 20 have 50-100, 21 have >100
- Largest handler: 541 lines (handler 91)
- Average handler: 35.5 lines

### Exit Targets

| Exit Target | Count | Meaning |
|---|---|---|
| @8 (continue) | 276 | Normal: proceed to next opcode |
| @6 (loop) | 2 | Restart opcode fetch loop |
| @5 (block) | 1 | Exit dispatch |
| @7 (if) | 1 | Re-enter if body |
| @4 (block) | 1 | Early exit |
| @2 (inner loop) | 1 | Continue inner loop |
| @1 (outer loop) | 1 | Continue outer loop |

### Cross-Handler Jumps

Only **2 cross-handler jumps** exist (both from handler 22):
- Handler 22 → handler 275 (block @10)
- Handler 22 → handler 276 (block @9)

These are easily handled via return codes if they end up in different groups.

## Splitting Strategy: "Stub Dispatch + Group Functions"

### Overview

1. **Main func 482** keeps the loop structure and br_table, but replaces
   handler code with tiny stubs that set a handler index
2. After dispatch, main function spills locals to memory, calls the
   appropriate group function, reloads locals, handles continuation
3. **5 group functions** each handle ~55 handlers with the actual opcode logic

### Main Function Transformation

```wasm
;; BEFORE the dispatch block @8:
;;   spill all 34 locals to stack frame memory (at offset 1280+)

;; REPLACE each handler body (between dispatch block ends) with:
;;   i32.const HANDLER_INDEX
;;   local.set handler_idx_local
;;   br (;@8;)   ;; jump to common exit

;; AFTER block @8 ends:
;;   compute group = handler_idx / GROUP_SIZE
;;   call $handler_group_N(frame_ptr, handler_idx)
;;   reload all 34 locals from memory
;;   handle continuation code (0=continue, 1..7=structural branches)
```

### Group Function Structure

```wasm
(func $handler_group_N (param $frame i32) (param $handler_idx i32) (result i32)
  (local ...34 locals matching original types...)
  (local $result i32)

  ;; LOAD locals from spill area in memory
  local.get $frame
  i32.load offset=SPILL_BASE+0
  local.set 2   ;; = original local 0
  ... (34 loads)

  ;; DISPATCH within group
  block $exit
    block $continue
      block $d_{G_size-1}
        ...
        block $d_0
          local.get $handler_idx
          i32.const G_START
          i32.sub
          br_table $d_0 $d_1 ... $d_{G_size-1} $continue
        end $d_0
        ;; handler G_START code (with transformed br targets)
      end $d_1
      ;; handler G_START+1 code
      ...
    end $continue
    ;; STORE locals back, return 0 (continue)
    ...
    i32.const 0
    return
  end $exit
  ;; STORE locals back, return $result
  ...
  local.get $result
)
```

### Depth Adjustment Formula

For group [G_start, G_end), the depth adjustment constant is:

    adj = 277 - G_end

All br/br_if to external targets (outside the group's dispatch blocks):
- **new_depth = old_depth - adj**
- @8 → maps to `$continue` (new_depth = old_depth - adj)
- @7 through @1 → maps to `$exit` (new_depth = old_depth - adj), plus set result local

Within-group dispatch targets: **depths unchanged** (relative positions preserved).

### Memory Layout for Locals Spill

Stack frame pointer = local 14. Current frame: 1280 bytes.
Extended to **1440 bytes** (+160 for spill area, aligned).

```
Offset from local 14:
  0-1279:     original stack frame usage
  1280-1283:  local 0 (i32, param 0)
  1284-1287:  local 1 (i32, param 1)
  1288-1291:  local 2 (i32)
  ...
  1400-1403:  local 30 (i32)
  1404-1407:  local 31 (i32)
  1408-1415:  local 32 (i64)
  1416-1423:  local 33 (i64)
  1424-1427:  handler_idx (i32) — passed to group function
```

New stack frame allocation: `i32.const 1440` (was 1280).

### Continuation Codes

| Code | Meaning | Main function action |
|---|---|---|
| 0 | Continue (was br @8) | br @8 (loop @6 continues) |
| 1 | Outer loop (was br @1) | br @1 |
| 2 | Inner loop (was br @2) | br @2 |
| 3 | Block @4 exit | br @4 |
| 4 | Block @5 exit | br @5 |
| 5 | Loop @6 continue | br @6 |
| 6 | If @7 re-enter | br @7 |
| 100+N | Re-dispatch to handler N | spill + call correct group |

### Correctness Constraints

1. All 34 locals spilled/reloaded on every handler invocation
2. Wasm operand stack empty at spill/call/reload boundaries
3. Memory and globals shared (linear memory is the same module)
4. Handler internal control flow (nested blocks/loops) preserved exactly
5. Cross-handler jumps (handler 22 → 275/276) handled via return codes

### Size Estimates

| Component | Lines | Est. binary bytes |
|---|---|---|
| Main func 482 (rewritten) | ~800 | ~3,000 |
| Each group function (~55 handlers) | ~2,200 | ~5,000 |
| 5 group functions total | ~11,000 | ~25,000 |
| **Total** | **~11,800** | **~28,000** |

Each individual function is well under 64KB and should be under the 8KB
HotSpot C2 JIT threshold (main function ~3KB, groups ~5KB each).

## Implementation Plan

1. Write Python script: `split_func482.py`
2. Input: `wasm-lib/libsqlite3.wasm`
3. Process: wasm2wat → parse → transform → wat2wasm
4. Output: `wasm-lib/libsqlite3_split.wasm`
5. Build sqlite4j with the split wasm
6. Run tests for correctness
7. Run perf-test/bench.sh for performance comparison
