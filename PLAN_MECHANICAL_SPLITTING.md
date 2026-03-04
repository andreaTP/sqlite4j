# Plan: Analyzing Mechanical WASM Function Splitting

## Goal

Determine how much of the function splitting process can be made fully automatic,
what conditions trigger it, and how it works in practice. The end state is a clear
specification for either a fully automated tool or a well-defined manual process
with minimal guesswork.

## Context

Today we manually configured splits for 3 functions (func_482, func_180, func_1177)
and achieved ~38% speedup. The process required:
- Profiling to identify hot functions
- Manual WAT inspection to extract 10+ config parameters per function
- Trial-and-error on group counts (bytecode size ≠ WAT line count)
- Debugging a failure case (func_1345: 6 test errors from 31 structural labels)

The question is: can we turn this into `python3 split_wasm.py --auto input.wasm output.wasm`?

---

## Part 1: What Triggers Splitting

### The decision rule (currently manual)

A function should be split when:
1. Its Java bytecode exceeds 8KB (the C2 JIT threshold)
2. It is hot enough that C2 compilation would help

Condition (1) can only be checked AFTER Chicory AOT compilation, not from the WASM
alone. WAT line count is a rough proxy but the ratio varies 2-5x across functions.

Condition (2) requires runtime profiling or can be approximated by call-graph analysis.

### What to investigate

- **Can we predict bytecode size from WAT?** Analyze the correlation between WAT
  line count and bytecode size for all 2712 functions. If there's a reliable ratio
  (or per-instruction-type weights), we can predict which functions will exceed 8KB
  without compiling first.

- **Can we skip the hotness check?** If splitting a cold function has near-zero cost
  (no spill/reload overhead because it's never called), then splitting ALL >8KB
  functions might be safe. The risk is the JIT compiler spending time compiling pieces
  of cold functions — but if they're truly cold, they won't reach the compilation
  threshold anyway.

- **Feedback loop approach:** Compile once with Chicory, measure bytecode sizes from
  the class file, then split only functions that exceed 8KB, recompile, and verify.
  This is a 2-pass process but is fully deterministic.

### Action items

1. ✅ Write a script that parses the compiled class file and lists all `func_*` methods
   with their bytecode sizes. → `check_bytecodes.py`
2. ✅ Correlate WAT line counts with bytecode sizes for all 2649 functions.
   → `correlate_wat_bytecode.py` — Results:
   - **Median ratio: 2.71 bytes/WAT-line**, StdDev 0.44, very tight P10-P90: 2.14-3.18
   - **WAT threshold of 2500 lines catches all 9 >8KB functions** (5 false positives)
   - **WAT threshold of 2000 lines catches all 9 >8KB functions** (8 false positives)
   - Functions >8KB have ratios 2.50-2.97 (all within 1 StdDev of median)
3. ✅ Safe WAT-based heuristic: **split if >2500 WAT lines**
   - Catches 100% of >8KB functions
   - Only 5 false positives (functions that are <8KB but >2500 WAT lines)
   - Conservative formula: `estimated_bytecode ≈ WAT_lines × 3.0` (P75 ratio)

---

## Part 2: What Can Be Detected Automatically from WAT

Currently, each function config requires these manually-specified fields:

| Field | Currently | Could be auto-detected? |
|-------|-----------|------------------------|
| `func_index` | manual | YES — from bytecode size scan |
| `strategy` | manual | PROBABLY — see below |
| `param_types` | manual | YES — parse `(param ...)` in WAT |
| `declared_types` | manual | YES — parse `(local ...)` in WAT |
| `frame_pointer_local` | manual | YES — pattern: `global.get 0` / `i32.const N` / `i32.sub` / `local.tee K` |
| `orig_frame_size` | manual | YES — the `i32.const N` in the frame setup |
| `opcode_local` | manual | YES — the `local.tee K` immediately before `br_table` |
| `handler_container_label` | manual | YES — the block containing all br_table target blocks |
| `num_structural` | manual | YES — count labels outside the handler container |
| `structural_types` | manual | YES — parse block/loop/if types from WAT |
| `num_groups` | manual trial-and-error | PARTIALLY — see below |
| `extraction_depth` | manual | HEURISTIC — find depths with large blocks |
| `min_block_size` | manual | HEURISTIC — 200 lines works for current cases |

**Almost everything can be auto-detected from the WAT.** The two exceptions are:
- `num_groups` — requires knowing bytecode size, which we don't have pre-compilation
- `strategy` — requires classifying the function structure

### Strategy detection

A function should use `br_table` strategy when:
- It contains a `br_table` with many entries (>10)
- The br_table targets are within a "handler container" block
- The function has a dispatch loop pattern (loop → br_table → handlers → branch back)

A function should use `block_extraction` strategy when:
- It does NOT have a large br_table dispatch pattern
- It has large blocks at specific nesting depths that can be extracted

Detection algorithm:
```
1. Find all br_table instructions in the function
2. For each br_table, count distinct handler blocks (targets within the same container)
3. If any br_table has >10 handler blocks → br_table strategy
4. Otherwise → block_extraction strategy
```

### Action items

4. ✅ Implement `auto_detect_config(wat_lines, func_start, func_end)` that returns a
   complete config dict by parsing the WAT. → `auto_detect_config.py`
   - Validated against all 4 known-good configs (func_482, func_180, func_1345, func_1177) — ALL PASS
   - Detects: param_types, declared_types, frame_pointer_local, orig_frame_size,
     strategy, opcode_local, handler_container_label, num_structural, structural_types
   - Identifies 14 candidate functions >2500 WAT lines
   - 6 use br_table strategy, 8 use block_extraction
5. ✅ For br_table strategy: handler container detection implemented using:
   - Staircase detection (consecutive block labels ending at @max_label)
   - Wrapper detection (if preceded by a loop with code gap, skip the structural wrapper)
6. ✅ For block_extraction strategy: automatic depth/size selection → `auto_detect_config.py`
   - "Fan-out depth" heuristic: finds shallowest depth with ≥2 blocks ≥ min_block_size
   - If any block at primary depth > 2500 WAT lines, adds deeper depths for sub-extraction
   - Default min_block_size = 200 (matches known-good func_1177 config)
   - Validated against func_1177: auto-detects extraction_depth=4, min_block_size=200 ✓
   - Also detects has_result field for all strategies
   - All 8 block_extraction candidates now have auto-detected configs

---

## Part 3: The Group Count Problem

This is the hardest part to automate. The number of groups determines whether each
piece fits under 8KB bytecode. But we can't know bytecode size until after Chicory
compiles the result.

### Current reality

- func_482 (278 handlers): needed 8 groups (started at 5, iterated up)
- func_180 (186 handlers): needed 12 groups (started at 4, iterated through 6, 10, 12)
- The ratio of bytecode-per-handler varies widely between functions and even between
  handler groups within the same function (3,105B to 7,332B across func_482's 8 groups)

### Approaches to investigate

**A) Overshoot and accept the overhead**
Use `ceil(original_bytecode / 4000)` groups instead of `ceil(original_bytecode / 8000)`.
This doubles the safety margin. The cost is more groups = more spill/reload calls,
but each is cheaper since pieces are smaller. Need to measure whether the overhead
matters.

**B) WAT-line-based estimation**
Count WAT lines per handler, compute a weighted estimate of bytecode size, and set
groups accordingly. This requires calibrating the WAT-to-bytecode ratio per
instruction type.

**C) Iterative compilation loop**
```
1. Start with min_groups = ceil(estimated_bytecode / 8000)
2. Split, compile with Chicory, check bytecodes
3. If any piece >8KB: increase groups, goto 2
4. If all pieces <8KB: done
```
This is the most reliable approach but requires running Chicory compilation in the
loop, which is slow (~15s per iteration).

**D) Hybrid: split at WASM level, verify at Java level**
```
1. Split with generous group count (approach A)
2. Compile once
3. Check: are all pieces <8KB? If yes, done.
4. If not, increase specific group counts and repeat.
```

### Action items

7. ✅ Measure the performance impact of "too many groups" — func_482 overshoot test:
   - 8 groups: best 19,517ms (avg 21,260ms) — optimal
   - 12 groups: best 22,124ms (avg 22,411ms) — **+13% vs optimal**
   - 16 groups: best 23,928ms (avg 24,773ms) — **+23% vs optimal**
   - **Conclusion: overshooting is NOT free.** 2x overshoot costs ~23% on the hottest
     function. Moderate overshoot (~1.5x) costs ~13%. Both still beat unsplit baseline
     (39,375ms) by a wide margin, so a small safety margin is OK but large overshoot
     is too expensive. The iterative compile loop (approach C/D) is needed for optimal
     results.
8. ✅ Implemented `--auto` mode in `split_wasm.py`:
   - `auto_detect_all_configs()` scans WAT for all functions >2500 lines
   - Auto-detects strategy, params, locals, frame setup, handler structure
   - Group count heuristic: `ceil(WAT_lines × 3.0 / 4000) + 1` — uses 4KB target
     per group (conservative, because undershoot causes correctness bugs while
     +13% overshoot penalty is acceptable)
   - `--skip=idx1,idx2,...` to exclude functions that fail (typed blocks, etc.)
   - Skips functions where auto-detection returns None for critical fields
   - Successfully splits 7 functions, all 432 tests pass
   - Functions still needing work: func_1194 (helper too large), func_1260 (helper too large)
   - Skipped functions: func_921, func_1184, func_2482, func_1936, func_1517, func_103, func_1146
     (cold or block_extraction with typed blocks causing wat2wasm type errors)
9. Build a WAT-to-bytecode estimator (approach B) by analyzing the compiled output
   for all functions and computing instruction-level weights.
   (Lower priority — the 4KB heuristic works well enough for now.)

---

## Part 4: Failure Modes to Handle

### func_1345: Many structural labels (31 labels) — FIXED

The br_table strategy failed with 6 test errors ("out of bounds memory access:
attempted to access address: -16"). The root cause was NOT the branch depth formula
(verified correct for S=31) but the **br_table-inside-handler** transformation.

**Root cause:** Handler 1 contained 3 cascading br_tables with entries targeting
different structural labels (@10, @18, @19, @20, @25, @28, @30, etc.). The original
`transform_handler_code` mapped ALL structural/cross-group entries to the `$exit`
depth WITHOUT setting continuation codes. The group function returned 0 (no
continuation), causing the main function to exit the dispatch loop instead of
branching to the correct structural label.

**Why func_482/func_180 worked:** Their individual handlers use `br`/`br_if` for
structural exits (not br_table), which correctly set cont_code via the existing
simple-branch transformation.

**Fix:** Wrapper landing blocks with save/reload pattern:
1. Classify each br_table entry as internal, within_group, container, structural,
   or cross_group
2. For structural/cross_group entries, create K wrapper landing blocks
3. Save dispatch value to `result_local_idx` before wrapper blocks (avoids WASM
   block stack isolation — blocks hide enclosing stack values)
4. Reload dispatch value inside innermost wrapper
5. Each landing block sets its continuation code and branches to `$exit`

**Result:** All 432 tests pass. No structural label limit — the fix handles
arbitrary numbers of structural labels correctly.

### func_1194: Pieces still >8KB after extraction

Block extraction produced a dispatcher (11,301B) and helper (11,961B) both exceeding
8KB. The dispatcher is large because the remaining code after extraction plus the
spill/reload overhead for 4 extraction points (41 locals × 6 instructions × 4 points)
is substantial.

**To investigate:**
- Extract more, smaller blocks (lower min_block_size, add more extraction depths)
- Nested extraction: extract blocks from within already-extracted helpers
- Use br_table strategy instead, if the function has a suitable dispatch pattern
  (it has 7 br_tables — but are any of them the "main" dispatch loop?)
- Measure: how small do extraction targets need to be to keep helpers under 8KB?
  (Rule of thumb: each WAT line → ~3 bytes of bytecode, so max ~2600 WAT lines
  per helper.)

### `return` instructions in extracted blocks

The block extraction strategy currently cannot handle blocks that contain `return`
instructions, because `return` has no continuation code equivalent. A return inside
a helper function would return from the helper, not from the original function.

**To investigate:**
- How many >8KB functions contain return instructions in extractable blocks?
- Can we transform `return` into a special continuation code that the caller
  interprets as "return immediately"?

### Action items

10. ✅ Debug func_1345 failure: FIXED — br_table-inside-handler entries now correctly
    set continuation codes via wrapper landing blocks with save/reload pattern.
11. For func_1194: try br_table strategy (inspect the 7 br_tables to find a main
    dispatch pattern) and try extracting at more depths.
12. Survey all >8KB functions for `return` instructions in potential extraction blocks.

---

## Part 5: Implementation in Binaryen (LATER — after rules are nailed down)

**Important:** Do NOT start with Binaryen. Stay in Python for the exploration phase.
The splitting rules, edge cases, and group-count strategy are still being discovered.
Python's edit-run-check cycle is 10x faster than C++ for this kind of iteration.
Move to Binaryen only after the rules are clear enough to write a spec.

Additionally, the spill/reload mechanism is Chicory-specific (saves WASM locals to
linear memory to pass them between Java methods). This is not a general WASM
optimization — it's a workaround for JVM bytecode size limits. A Binaryen pass
would need to be designed around this JVM-specific quirk.

The production implementation of mechanical splitting should eventually be done as a
Binaryen pass. Binaryen (`/home/andreatp/workspace/binaryen`) is the standard WASM
optimization toolchain and has infrastructure for function-level transformations.

### Why Binaryen (when the time comes)

- **Proper IR**: Binaryen operates on a structured IR, not text. No regex parsing of
  WAT, no off-by-one errors in label depth calculations (the likely cause of the
  func_1345 failure).
- **Existing passes**: Binaryen already has passes for inlining, dead code elimination,
  local optimization. A splitting pass fits naturally.
- **Composable**: Users can run `wasm-opt --split-large-funcs -o output.wasm input.wasm`
  as part of their build pipeline.
- **Type-safe transformations**: Creating new functions, adjusting branch targets, and
  managing locals are all first-class operations in the Binaryen API.
- **Reusable**: Benefits any WASM-to-JVM pipeline, not just sqlite4j.

### Exploration plan (in /home/andreatp/workspace/binaryen)

1. **Study existing passes** — Read a simple pass (e.g., `src/passes/Inlining.cpp` or
   `src/passes/MergeBlocks.cpp`) to understand the pass infrastructure: how to iterate
   functions, create new functions, modify function bodies, manage types.

2. **Study the IR** — Understand how Binaryen represents:
   - Function bodies (`Expression*` tree)
   - `Switch` (br_table equivalent)
   - `Block`, `Loop`, `If` nesting
   - Local variables and their types
   - Branch targets and label resolution

3. **Prototype the detection** — Write a pass that:
   - Walks all functions
   - Estimates output size (Binaryen has `BinaryenGetExpressionInfo` / size estimation)
   - Identifies functions with `Switch` (br_table) dispatch patterns
   - Logs candidates: function index, estimated size, handler count, local count

4. **Prototype the split** — For br_table functions:
   - Create N new functions with the same local types + 2 extra params (frame_ptr, handler_idx)
   - Move handler expressions from the original Switch into group functions
   - Replace handlers in the original with stubs (set index, break out)
   - Add spill/reload/dispatch logic to the original
   - Register new functions in the module

5. **Test against sqlite4j** — Run the Binaryen pass on `libsqlite3.wasm`, then use
   the output in sqlite4j's build. Verify all 432 tests pass and benchmark.

### Key Binaryen files to study

```
src/pass.h                    — Pass infrastructure
src/passes/                   — All existing passes
src/passes/Inlining.cpp       — Creates new functions, moves code between them
src/passes/FuncCastEmulation.cpp — Wraps functions, relevant pattern
src/wasm.h                    — Core IR types (Function, Expression, Block, Switch, etc.)
src/wasm-builder.h            — Builder API for constructing IR nodes
src/ir/branch-utils.h         — Branch target manipulation utilities
src/ir/local-utils.h          — Local variable utilities
src/ir/utils.h                — Expression walking, cloning, replacement
```

### What changes vs the Python approach

| Aspect | Python (split_wasm.py) | Binaryen pass |
|--------|----------------------|---------------|
| Input format | WAT text | Binary WASM / structured IR |
| Branch handling | Regex + manual depth tracking | IR-level, type-checked |
| New function creation | String concatenation | API: `module->addFunction(...)` |
| Label resolution | Manual relative depth math | Automatic via IR |
| Size estimation | Post-hoc (compile, check) | Binaryen's built-in size estimation |
| Error-prone parts | Label depth, local index offset | Minimal — IR handles these |
| Reusability | sqlite4j only | Any WASM project |

### Action items

13. Explore Binaryen pass infrastructure: read 2-3 existing passes, understand the API.
14. Prototype a "detect large functions" pass that logs candidates.
15. Prototype br_table splitting for a single function (func_482) in Binaryen.
16. Compare generated output against split_wasm.py output for correctness.
17. Extend to handle block_extraction strategy.
18. Add `--split-large-funcs` flag or similar to wasm-opt.

---

## Part 6: End-to-End Automation Sketch

Whether implemented in Python (short term) or Binaryen (long term), the fully
automated flow is:

```
auto_split(input_wasm, output_wasm, bytecode_threshold=8000):
  1. Analyze all functions: estimate size, detect strategy
  2. For each function likely to exceed threshold:
       - Detect config automatically (params, locals, frame_ptr, br_table structure)
       - Choose initial group count (generous estimate)
       - Apply split
  3. Compile output with Chicory, measure actual bytecode sizes
  4. For any piece still >threshold:
       - Increase that function's group count
       - Re-split and recompile
  5. Validate: wasm-validate + test suite
```

The manual steps that remain hard to automate:
- Choosing optimal group counts without compilation feedback (mitigated by step 4)
- Handling edge cases (many structural labels, return in blocks)
- Deciding whether a function is "hot enough" to justify splitting overhead

---

## Execution Order

| Priority | Task | Where | Why |
|----------|------|-------|-----|
| 1 | Extract bytecode size checker into reusable script (item 1) | sqlite4j | Foundation for everything else |
| 2 | Correlate WAT lines with bytecode sizes (item 2) | sqlite4j | Determines if we can predict sizes |
| 3 | Explore Binaryen pass infrastructure (item 13) | binaryen | Understand the target platform |
| 4 | Implement auto_detect_config for br_table in Python (items 4-5) | sqlite4j | Quick prototype, validates the logic |
| 5 | Debug func_1345 failure (item 10) | sqlite4j | Understand structural label limits |
| 6 | Prototype "detect large functions" Binaryen pass (item 14) | binaryen | First Binaryen deliverable |
| 7 | Test "overshoot" group count strategy (item 7) | sqlite4j | If cheap, simplifies group selection |
| 8 | Prototype br_table splitting in Binaryen (item 15) | binaryen | Core Binaryen deliverable |
| 9 | Tackle func_1194 (item 11) | sqlite4j | Third-hottest function |
| 10 | Full Binaryen pass with iterative sizing (items 16-18) | binaryen | Production-ready tool |
