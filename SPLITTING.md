# WASM Function Splitting

## Overview

SQLite's VDBE (Virtual Database Engine) main loop compiles to a single 24KB WASM function (`func 482`). When Chicory's AOT compiler translates this to Java bytecode, the resulting method exceeds HotSpot's 8KB C2 JIT compilation threshold, falling back to the slower C1 compiler or interpreter. The `split_func482.py` script transforms the WASM binary to split this function into a thin dispatcher plus 5 group functions, each small enough for C2 JIT compilation.

## How It Works

The transformation operates at the WASM text format (WAT) level:

1. **Disassemble** the input `.wasm` to `.wat` using `wasm2wat`
2. **Parse** func 482 to identify the `br_table` dispatch, 278 opcode handlers, and structural control flow
3. **Replace** the handler code in func 482 with stubs that record a handler index and exit the dispatch
4. **Generate** 5 group functions that contain the actual handler logic
5. **Reassemble** the modified WAT back to `.wasm` using `wat2wasm`

Locals are passed between the main function and group functions by spilling/reloading them through an extended region of the stack frame in linear memory.

## Architecture

Before splitting:

```
func 482 (~24KB)
├── Setup (stack frame, field loads, init)
├── loop @1 (outer loop)
│   └── loop @2 (inner loop)
│       └── block @3 → block @4 → block @5 → loop @6 → if @7
│           └── block @8 (handler block)
│               ├── 277 nested dispatch blocks (@9..@285)
│               ├── br_table (opcode → handler)
│               └── 278 opcode handlers (inline)
└── Structural code (cleanup, loop control, epilogue)
```

After splitting:

```
func 482 (~3KB)                     group_0 (~5KB)    group_1..4 (~5KB each)
├── Setup                           ├── Load locals    ...
├── Spill locals to memory          ├── br_table dispatch
├── loop @1 / loop @2 / ...         ├── Handler 0 code
│   └── block @8                    ├── Handler 1 code
│       ├── 277 dispatch blocks     ├── ...
│       ├── br_table (unchanged)    ├── Handler 55 code
│       └── 278 stubs:              ├── Store locals
│           set handler_idx, br @8  └── Return continuation code
├── Post-spill fix (local 28)
├── loop $redispatch
│   ├── Call group function
│   ├── Handle cross-group jumps
│   └── end $redispatch
├── Reload locals from memory
├── Handle continuation code
└── Structural code (unchanged)
```

## Transformation Details

Four key transformations are applied to handler code when moving it into group functions:

### 1. Local Index Offset

Group functions receive two parameters (`$frame: i32`, `$handler_idx: i32`) that occupy local indices 0 and 1. All original local references are shifted by +2:

```
local.get N  →  local.get N+2
local.set N  →  local.set N+2
local.tee N  →  local.tee N+2
```

### 2. Branch Depth Adjustment for `$continue`

In the original function, `br` to label `@8` means "continue to next opcode." In a group function, `@8` no longer exists. Instead, the depth is recomputed to target the group function's `$continue` block:

```
Original:  br (277 - H)       ;; where H = handler index within the original function
Group:     br (G_size - 1 - H_local + internal_depth)  ;; H_local = H - group_start
```

### 3. Structural Exit Conversion

Branches to structural labels (`@7` through `@1`) cannot be expressed as direct jumps from group functions. They are converted to: set a continuation code in the `$result` local, then branch to `$exit`:

```wasm
;; Original:   br <depth_to_@6>
;; Becomes:
i32.const 5          ;; continuation code for @6
local.set 36         ;; $result (local index 2 + 34 original locals)
br <depth_to_$exit>
```

For `br_if`, the pattern wraps in an `if/end` block (adding +1 to the exit depth).

### 4. Cross-Group Jump Handling

Handler 22 contains jumps to handlers 275 and 276, which may reside in a different group. These are encoded as continuation code `100 + target_handler_index` and branch to `$exit`. The main function's `$redispatch` loop detects codes >= 100, subtracts 100 to recover the target handler index, and re-calls the appropriate group function.

## Spill/Reload Mechanism

The original func 482 has 34 locals: 2 `i32` params + 30 declared `i32` + 2 declared `i64`. These are passed to group functions through an extended region of the stack frame.

### Memory Layout

The original stack frame is 1280 bytes. The transformation extends it to 1440 bytes (+160 for the spill area):

| Offset | Local | Type | Size |
|---|---|---|---|
| 1280 | local 0 | i32 | 4 bytes |
| 1284 | local 1 | i32 | 4 bytes |
| ... | ... | ... | ... |
| 1400 | local 30 | i32 | 4 bytes |
| 1404 | local 31 | i32 | 4 bytes |
| 1408 | local 32 | i64 | 8 bytes |
| 1416 | local 33 | i64 | 8 bytes |

The frame pointer is `local 14` in the original function. Every call to a group function is bracketed by:

1. **Spill** — store all 34 locals from the main function to `[frame + 1280..]`
2. **Call** — pass the frame pointer and handler index
3. **Group function loads** locals from the spill area on entry, **stores** them back before returning
4. **Reload** — the main function loads all 34 locals back from the spill area

## The Local 28 Fix

Inside the dispatch blocks (between the spill and `block @8`'s end), the original code executes `local.tee 28` to set the opcode index. This happens *after* the locals have already been spilled to memory, so the spill area would contain a stale value for local 28.

A post-dispatch store fixes this:

```wasm
;; Right after block @8 closes, before calling the group function:
local.get 14              ;; frame pointer
local.get 28              ;; opcode index (set by local.tee 28 in dispatch)
i32.store offset=1392     ;; SPILL_BASE + 28 * 4
```

## Usage

### Prerequisites

Install the [WebAssembly Binary Toolkit (WABT)](https://github.com/WebAssembly/wabt):

- `wasm2wat` — disassembles `.wasm` to `.wat` text format
- `wat2wasm` — assembles `.wat` back to `.wasm` binary

### Running the Script

```bash
python3 split_func482.py wasm-lib/libsqlite3.wasm wasm-lib/libsqlite3_split.wasm
```

The script:
1. Converts the input WASM to WAT (temporary file)
2. Parses and transforms func 482
3. Writes the modified WAT to `wasm-lib/libsqlite3_split.wat`
4. Compiles it to `wasm-lib/libsqlite3_split.wasm` via `wat2wasm`
5. Reports input/output sizes and overhead

### Build Integration

The Maven build (`pom.xml`) is configured to use the split WASM via the Chicory compiler plugin:

```xml
<wasmFile>${project.basedir}/wasm-lib/libsqlite3_split.wasm</wasmFile>
```

No additional build steps are required once `libsqlite3_split.wasm` has been generated.

## Configuration

Tunable parameters are defined at the top of `split_func482.py`:

| Parameter | Default | Description |
|---|---|---|
| `NUM_GROUPS` | 5 | Number of group functions to generate. Handlers are divided equally across groups (~56 handlers each). |
| `SPILL_BASE` | 1280 | Byte offset within the stack frame where the local spill area begins. Must not overlap with the original frame's usage. |
| `NEW_FRAME_SIZE` | 1440 | Extended stack frame size in bytes. Must be >= `SPILL_BASE` + (32 * 4) + (2 * 8) = 1424. |
| `FUNC_482_INDEX` | 482 | WASM function index of the VDBE main loop. |
| `NUM_IMPORTS` | 58 | Number of imported functions in the WASM module (affects function index calculation). |
| `CONTINUATION_CODES` | (dict) | Maps structural label numbers to continuation return codes used by the main function's dispatch logic. |

## Verification

### Test Suite

Run the project's full test suite to verify the split WASM produces identical behavior:

```bash
mvn test
```

All existing tests exercise SQLite through the split VDBE function. Any handler transformation error will surface as a test failure.

### WASM Validation

Validate the output binary with WABT's validator:

```bash
wasm-validate wasm-lib/libsqlite3_split.wasm
```

This checks structural correctness (type mismatches, invalid branch depths, malformed sections) but not semantic equivalence.

### Script Output

The script itself prints diagnostic information:

```
[1/6] Converting ... to WAT
[2/6] Parsing func 482...
  br_table at line ...
  278 opcode handlers
  8 structural code regions
[3/6] Computing group boundaries...
  Group 0: handlers [0, 56) = 56 handlers
  Group 1: handlers [56, 112) = 56 handlers
  ...
[4/6] Generating group functions...
[5/6] Generating modified func 482...
[6/6] Assembling output WAT...

  Input size:  ... bytes
  Output size: ... bytes
  Overhead:    ... bytes (...)
```

Review the handler counts, group sizes, and binary overhead to confirm the transformation completed as expected.
