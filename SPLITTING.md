# WASM Function Splitting

## Overview

When Chicory's AOT compiler translates WASM functions to Java bytecode, methods larger than ~8KB exceed HotSpot's C2 JIT compilation threshold, falling back to the slower C1 compiler or interpreter. The `split_wasm.py` script transforms the WASM binary to split oversized functions into smaller pieces, each fitting within the C2 threshold.

Three functions are split:

| Function | Original Size | Strategy | Pieces | Description |
|----------|--------------|----------|--------|-------------|
| func 482 | ~24KB | br_table dispatch | 1 dispatcher + 5 groups | VDBE main loop (278 opcode handlers) |
| func 180 | ~31KB | br_table dispatch | 1 dispatcher + 4 groups | Expression evaluator (186 opcode handlers) |
| func 1194 | ~21KB | block extraction | 1 main + 4 helpers | Query optimizer (deeply nested blocks) |

Func 1177 (~6.5KB) was analyzed but is already under the 8KB threshold, so it is not split.

## How It Works

The transformation operates at the WASM text format (WAT) level:

1. **Disassemble** the input `.wasm` to `.wat` using `wasm2wat`
2. **Parse** each target function to identify its structure
3. **Transform** each function using the appropriate strategy (see below)
4. **Append** generated helper/group functions at the end of the module
5. **Reassemble** the modified WAT back to `.wasm` using `wat2wasm`

Locals are passed between the main function and helper functions by spilling/reloading through an extended region of the stack frame in linear memory.

## Strategy A: br_table Dispatch (funcs 482, 180)

Used for functions that contain a `br_table` switch dispatching to many inline opcode handlers.

### Architecture

Before splitting:

```
func (original, ~24-31KB)
├── Setup (stack frame, field loads, init)
├── Dispatch structure (blocks, loops)
│   └── Handler container block
│       ├── N nested dispatch blocks
│       ├── br_table (opcode → handler)
│       └── N+1 opcode handlers (inline)
└── Structural code (cleanup, loop control, epilogue)
```

After splitting:

```
func (dispatcher, ~3KB)           group_0 (~5-8KB)    group_1..N (~5-8KB each)
├── Setup                         ├── Load locals      ...
├── Spill locals to memory        ├── br_table dispatch
├── Dispatch structure            ├── Handler 0 code
│   └── Handler container         ├── Handler 1 code
│       ├── Dispatch blocks       ├── ...
│       ├── br_table (unchanged)  ├── Store locals
│       └── Stubs:                └── Return continuation code
│           set handler_idx, br
├── Post-spill fix (opcode local)
├── loop $redispatch
│   ├── Call group function
│   ├── Handle cross-group jumps
│   └── end $redispatch
├── Reload locals from memory
├── Handle continuation code
└── Structural code (unchanged)
```

### Transformation Details

Four key transformations are applied to handler code when moving it into group functions:

#### 1. Local Index Offset

Group functions receive two parameters (`$frame: i32`, `$handler_idx: i32`) that occupy local indices 0 and 1. All original local references are shifted by +2:

```
local.get N  →  local.get N+2
local.set N  →  local.set N+2
local.tee N  →  local.tee N+2
```

#### 2. Branch Depth Adjustment for `$continue`

In the original function, `br` to the handler container means "continue to next opcode." In a group function, that label no longer exists. The depth is recomputed to target the group function's `$continue` block:

```
Original:  br (N_blocks - H)
Group:     br (G_size - 1 - H_local + internal_depth)
```

#### 3. Structural Exit Conversion

Branches to structural labels (enclosing loops, blocks, ifs) cannot be expressed as direct jumps from group functions. They are converted to: set a continuation code in the `$result` local, then branch to `$exit`:

```wasm
;; Original:   br <depth_to_structural_label>
;; Becomes:
i32.const <continuation_code>
local.set <result_local>
br <depth_to_$exit>
```

For `br_if`, the pattern wraps in an `if/end` block (adding +1 to the exit depth).

#### 4. Cross-Group Jump Handling

Some handlers contain jumps to handlers in other groups. These are encoded as continuation code `100 + target_handler_index` and branch to `$exit`. The main function's `$redispatch` loop detects codes >= 100, subtracts 100 to recover the target handler index, and re-calls the appropriate group function.

### Per-Function Details

#### Func 482 (VDBE main loop)

- **Locals:** 34 total (2 i32 params + 30 i32 + 2 i64)
- **Frame pointer:** local 14, frame size 1280 bytes
- **Handlers:** 278 → 5 groups of ~56 each
- **Opcode local:** 28 (post-dispatch fix required)

#### Func 180 (expression evaluator)

- **Locals:** 48 total (1 i32 param + 39 i32 + 6 i64 + 2 f64)
- **Frame pointer:** local 6, frame size 512 bytes
- **Handlers:** 186 → 4 groups of ~47 each
- **Opcode local:** 1 (post-dispatch fix required)
- **Key difference:** has f64 locals requiring `f64.load`/`f64.store` in spill/reload

## Strategy B: Block Extraction (func 1194)

Used for functions with deeply nested blocks but no single dispatch table. Large complete blocks are extracted into helper functions.

### Architecture

Before splitting:

```
func 1194 (~21KB)
├── Setup
├── Deep nesting (@1..@13)
│   ├── block @13 #1 (3,786 lines)    ← extracted
│   ├── if @13 #2 (509 lines)         ← extracted
│   ├── block @8 (2,155 lines)        ← extracted
│   └── block @7 (1,022 lines)        ← extracted
└── Epilogue
```

After splitting:

```
func 1194 (~8KB)                  helper_0 (~9KB)    helper_1..3 (~2-6KB)
├── Setup                         ├── Load locals     ...
├── Deep nesting                  ├── Execute block
│   ├── [spill → call helper_0    ├── Store locals
│   │    → reload → dispatch]     └── Return continuation code
│   ├── [spill → call helper_1
│   │    → reload → dispatch]
│   ├── [spill → call helper_2
│   │    → reload → dispatch]
│   └── [spill → call helper_3
│        → reload → dispatch]
└── Epilogue
```

### How It Works

1. **Identify extraction targets:** complete blocks at chosen nesting depths (7, 8, 13) that exceed a minimum size threshold
2. **Ancestor conflict resolution:** when targets are nested (ancestor contains descendant), keep the ancestor and remove the descendant
3. **For each target, generate a helper function:**
   - Parameters: `(frame_ptr: i32, block_idx: i32)` → result i32
   - Load locals from spill area on entry
   - Execute the extracted block code with transformed branches:
     - Internal branches: adjusted for new block structure
     - Branches to enclosing blocks: converted to set continuation code + `br $exit`
   - Store locals back before returning
   - Return continuation code (0 = normal fall-through)
4. **In the main function,** replace each extracted block with:
   - Spill locals to memory
   - Call helper function
   - Save continuation code to a spare local
   - Reload locals from memory
   - Dispatch on continuation code via `br_table` to the appropriate structural label

### Continuation Code Dispatch

Each helper returns an integer continuation code:

| Code | Meaning |
|------|---------|
| 0 | Normal exit (fall through to next code) |
| N | Branch to the Nth enclosing structural label |

The main function dispatches on this code using a `br_table`:

```wasm
;; After reload:
local.get <spare_local>          ;; saved continuation code
br_table 0 1 2 ... max max       ;; 0 = fall through, N = br to enclosing label
```

## Spill/Reload Mechanism

Both strategies use the same mechanism to pass locals between the main function and helpers. The original stack frame is extended with a spill area where all locals are stored/loaded.

### How It Works

1. **Spill** — the main function stores all locals to `[frame_ptr + spill_base..]`
2. **Call** — pass the frame pointer (and handler/block index) to the helper
3. **Helper loads** locals from the spill area on entry
4. **Helper stores** locals back to the spill area before returning
5. **Reload** — the main function loads all locals back from the spill area

### Type Support

The spill/reload mechanism supports all WASM numeric types:

| Type | Load Instruction | Store Instruction | Size |
|------|-----------------|-------------------|------|
| i32 | `i32.load` | `i32.store` | 4 bytes |
| i64 | `i64.load` | `i64.store` | 8 bytes |
| f32 | `f32.load` | `f32.store` | 4 bytes |
| f64 | `f64.load` | `f64.store` | 8 bytes |

### The Opcode Local Fix (br_table strategy only)

Inside the dispatch blocks, the original code executes `local.tee` to set the opcode index *after* locals have been spilled. A post-dispatch store fixes the stale value:

```wasm
;; Right after the handler container block closes:
local.get <frame_ptr>
local.get <opcode_local>
i32.store offset=<spill_base + opcode_local * 4>
```

## Usage

### Prerequisites

Install the [WebAssembly Binary Toolkit (WABT)](https://github.com/WebAssembly/wabt):

- `wasm2wat` — disassembles `.wasm` to `.wat` text format
- `wat2wasm` — assembles `.wat` back to `.wasm` binary

### Running the Script

```bash
python3 split_wasm.py wasm-lib/libsqlite3.wasm wasm-lib/libsqlite3_split.wasm
```

The script:
1. Converts the input WASM to WAT (temporary file)
2. Parses and transforms each target function (482, 180, 1194)
3. Appends generated helper/group functions at the end of the module
4. Compiles the modified WAT to the output `.wasm` via `wat2wasm`
5. Reports input/output sizes and overhead

### Build Integration

The Maven build (`pom.xml`) is configured to use the split WASM via the Chicory compiler plugin:

```xml
<wasmFile>${project.basedir}/wasm-lib/libsqlite3_split.wasm</wasmFile>
```

No additional build steps are required once `libsqlite3_split.wasm` has been generated.

## Configuration

Each target function has a configuration dict at the top of `split_wasm.py`. Key fields:

| Field | Description |
|-------|-------------|
| `func_index` | WASM function index |
| `strategy` | `'br_table'` or `'block_extraction'` |
| `param_types` | List of parameter types (e.g., `['i32', 'i32']`) |
| `declared_types` | List of declared local types |
| `frame_pointer_local` | Local index holding the frame pointer |
| `orig_frame_size` | Original stack frame size in bytes |

**br_table strategy additional fields:**

| Field | Description |
|-------|-------------|
| `num_groups` | Number of group functions to generate |
| `opcode_local` | Local set by `local.tee` before `br_table` |
| `handler_container_label` | Label number of the handler container block |
| `structural_types` | Maps label numbers to block types (loop/block/if) |

**block_extraction strategy additional fields:**

| Field | Description |
|-------|-------------|
| `extraction_depths` | List of nesting depths to look for extraction targets |
| `min_block_size` | Minimum block size (WAT lines) to consider for extraction |
| `max_block_size` | Maximum block size to extract (prevents oversized helpers) |

## Verification

### Test Suite

Run the project's full test suite to verify the split WASM produces identical behavior:

```bash
mvn test
```

All existing tests exercise SQLite through the split functions. Any transformation error will surface as a test failure.

### WASM Validation

Validate the output binary with WABT's validator:

```bash
wasm-validate wasm-lib/libsqlite3_split.wasm
```

This checks structural correctness (type mismatches, invalid branch depths, malformed sections) but not semantic equivalence.

### Binary Size Check

Verify that generated functions are under the 8KB threshold:

```bash
wasm2wat wasm-lib/libsqlite3_split.wasm | grep -c "func"  # count functions
wasm-objdump -h wasm-lib/libsqlite3_split.wasm             # section sizes
```
