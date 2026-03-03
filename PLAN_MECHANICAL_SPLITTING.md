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

1. Write a script that parses the compiled class file and lists all `func_*` methods
   with their bytecode sizes. (We already have this code inline — extract it.)
2. Correlate WAT line counts with bytecode sizes for all 2712 functions. Plot or
   compute the regression. Identify outliers.
3. Determine if there's a WAT-based heuristic that's safe (e.g., "split if >2000
   WAT lines" with enough margin that we never miss a >8KB function).

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

4. Implement `auto_detect_config(wat_lines, func_start, func_end)` that returns a
   complete config dict by parsing the WAT. Test it against the 3 known-good configs
   (func_482, func_180, func_1177) and verify it produces equivalent configs.
5. For br_table strategy: implement handler container detection (find the block that
   wraps all br_table target blocks).
6. For block_extraction strategy: implement automatic depth/size selection (find
   depths with extractable blocks that would reduce bytecode below 8KB).

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

7. Measure the performance impact of "too many groups" — compare func_482 with 8
   groups vs 12 groups vs 16 groups. If the overhead is small, overshooting is cheap.
8. Implement the iterative approach (C) as a fallback. Wire up: split → compile →
   check bytecodes → adjust → repeat.
9. Build a WAT-to-bytecode estimator (approach B) by analyzing the compiled output
   for all functions and computing instruction-level weights.

---

## Part 4: Failure Modes to Handle

### func_1345: Many structural labels (31 labels)

The br_table strategy failed with 6 test errors when the function had 31 structural
labels before the handler container. The branch transformation logic converts handler
branches to "continuation codes" that are dispatched via br_table in the caller. With
31 structural labels, the continuation code space and branch depth calculations may
overflow or miscalculate.

**To investigate:**
- Which specific branch transformation produces the wrong code?
- Run `wasm-validate` (it passed!) — so the bug is semantic, not structural.
- Add a WAT-level diff test: extract one handler, validate, run a targeted test.
- Consider: is there a structural limit on how many structural labels the splitter
  can handle? If so, document it and fall back to block_extraction.

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

10. Debug func_1345 failure: add targeted test, examine generated WAT for the failing
    handler, compare against original WAT.
11. For func_1194: try br_table strategy (inspect the 7 br_tables to find a main
    dispatch pattern) and try extracting at more depths.
12. Survey all >8KB functions for `return` instructions in potential extraction blocks.

---

## Part 5: End-to-End Automation Sketch

If all the above investigations succeed, the fully automated flow would be:

```
auto_split(input_wasm, output_wasm):
  1. wat = wasm2wat(input_wasm)
  2. for each function in wat:
       config = auto_detect_config(function)
       if config.estimated_bytecode > 8000:
         configs.append(config)
  3. split_wat = apply_splits(wat, configs)
  4. output_wasm = wat2wasm(split_wat)
  5. compile with Chicory, extract bytecode sizes
  6. for any piece >8KB:
       increase that function's group count
       goto 3
  7. validate: wasm-validate + test suite
```

The manual steps that remain hard to automate:
- Choosing optimal group counts without compilation feedback
- Handling edge cases (many structural labels, return in blocks)
- Deciding whether a function is "hot enough" to justify splitting overhead

### Action items

13. Prototype `auto_detect_config()` for br_table functions.
14. Prototype a bytecode-size-checking script that reads the Chicory-generated class.
15. Wire them together into a single `--auto` mode in split_wasm.py.

---

## Execution Order

| Priority | Task | Why |
|----------|------|-----|
| 1 | Extract bytecode size checker into reusable script (item 1) | Foundation for everything else |
| 2 | Correlate WAT lines with bytecode sizes (item 2) | Determines if we can predict sizes |
| 3 | Implement auto_detect_config for br_table (items 4-5) | Eliminates manual config writing |
| 4 | Debug func_1345 failure (item 10) | Understand structural label limits |
| 5 | Test "overshoot" group count strategy (item 7) | If cheap, simplifies group selection |
| 6 | Implement iterative compilation loop (item 8) | Reliable fallback for group sizing |
| 7 | Tackle func_1194 (item 11) | Third-hottest function |
| 8 | Wire up --auto mode (items 13-15) | Full automation |
