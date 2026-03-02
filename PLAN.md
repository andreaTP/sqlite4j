# Plan: Improve sqlite4j Module Execution Performance via Targeted Splitting

## Context

We asked Claude Code to split large WASM functions so they fit under HotSpot's ~8KB C2 JIT threshold when compiled to Java bytecode by Chicory AOT. The experiment produced a clear and somewhat surprising result:

| Scenario | What was split | Time | Delta |
|----------|---------------|------|-------|
| Baseline | nothing | ~8s | — |
| Split func 482 only | VDBE main loop → 6 pieces | ~7s | **-12.5%** |
| Split 482 + 180 + 1194 | 3 functions → 15 pieces | ~9s | **+28.6%** |

Splitting the single hottest function helped. Splitting more functions made things *worse* than baseline. The spill/reload overhead (copying all locals through linear memory on every helper call) dominates when a function isn't called frequently enough to recoup the cost through better JIT compilation.

**Goal:** Dramatically improve module execution performance by splitting only the right functions, in the right way, with minimal overhead.

## What We Learned

### The cost model

Each split introduces per-call overhead:
- **Spill:** `N_locals × 3` WASM instructions (load frame ptr, load local, store to memory)
- **Reload:** `N_locals × 3` WASM instructions (load frame ptr, load from memory, set local)
- **Call/return:** ~10 instructions (push args, call, save result, dispatch continuation)

For func 482 (34 locals): **~214 instructions of overhead per handler dispatch.**
For func 180 (48 locals): **~298 instructions of overhead per handler dispatch.**

A split is only profitable when:
```
(call_frequency × JIT_speedup_per_call) > (call_frequency × spill_reload_cost)
```

This means: **it's not about function size — it's about hotness × size.**

### What we don't know yet

1. **Which functions are actually hot?** We split 180 and 1194 based on size alone. We need profiling data showing call frequency.
2. **Is the spill/reload the bottleneck?** Or is it that C2 JIT for the helpers isn't actually faster than C1 for the unsplit monolith?
3. **Can we reduce the spill/reload cost?** Currently we spill ALL locals. Many may be dead at the call site.
4. **What does the C2 JIT actually do with our pieces?** We assumed "under 8KB = C2 compiles it" but we haven't verified this.

## Analysis Plan

### Step 1: Profile-guided function identification

Before splitting anything, identify the actual hot functions:

```bash
# Run with async-profiler in method-level mode
jbang --javaagent=ap-loader@jvm-profiling-tools/ap-loader=start,event=cpu,file=profile_methods.html perf-test/test
```

From the flame graph, extract:
- **Top 20 functions by self-time** — these are the ones where the CPU actually spends time
- **Top 20 functions by total-time** — these are the hot call paths
- **For each >8KB function:** what % of total execution time does it account for?

**Decision rule:** Only split a function if it accounts for >5% of total CPU time AND its binary size exceeds 8KB.

### Step 2: Verify C2 JIT compilation status

Confirm that our split pieces are actually being C2-compiled:

```bash
# Run with JIT compilation logging
java -XX:+PrintCompilation -XX:+UnlockDiagnosticVMOptions -XX:+PrintInlining ...
```

Check:
- Are the split helper methods (group_0, group_1, etc.) compiled by C2 or C1?
- Are the unsplit >8KB methods compiled by C1 only?
- Is C2 inlining any of our helpers into the caller?

### Step 3: Measure spill/reload overhead directly

Instrument or estimate the cost:
- Count how many times each split function's dispatch loop executes (add a counter or use perf)
- Multiply by the spill/reload instruction count
- Compare to total instruction count from perf stat

### Step 4: Evaluate selective spilling

Current approach spills ALL locals. Potential optimization:
- **Liveness analysis:** Which locals are actually live at each handler exit? Only spill those.
- **Read-only locals:** Parameters and constants don't change — spill once, skip on reload.
- **Partitioned spill:** Instead of spilling everything before calling the helper, pass the most-used locals as WASM function parameters (up to the WASM limit).

This is complex to implement but could dramatically reduce the per-call cost.

## Splitting Constraints

### Hard constraints (must satisfy)

1. **Each generated Java method must be ≤8KB of bytecode** for C2 JIT eligibility
2. **WASM module must validate** — all branch depths, types, and function signatures correct
3. **Behavioral equivalence** — all 432 tests must pass identically
4. **No return instruction in extracted blocks** — the current block extraction strategy cannot handle mid-block returns

### Soft constraints (should satisfy for net performance gain)

1. **Only split functions that are hot** — profiling must show >5% CPU time
2. **Minimize number of pieces** — each additional piece adds overhead; split into the fewest groups that keep each piece under 8KB
3. **Minimize locals count** — functions with fewer locals have cheaper spill/reload; prefer splitting functions with <30 locals
4. **Prefer br_table dispatch functions** — the stub pattern is clean and the overhead is amortized across many handlers
5. **Avoid splitting functions called in tight loops from other split functions** — cascading spill/reload is multiplicative

### How to decide group count

```
min_groups = ceil(original_binary_size / 8192)
```

Start with `min_groups` and increase only if individual groups exceed 8KB. More groups = more overhead, so use the minimum.

## How to Perform a Split

### For br_table dispatch functions (like func 482)

1. **Find the function** in WAT, locate the br_table and handler container block
2. **Count handlers** and divide into `min_groups` groups
3. **Replace handlers with stubs** that record the handler index and exit the container
4. **Generate group functions** with the handler code, transforming:
   - Local indices: +2 offset (for frame_ptr and handler_idx params)
   - $continue branches: recomputed for group's block structure
   - Structural exits: converted to continuation codes
   - Cross-group jumps: encoded as continuation code 100+target
5. **Add spill/reload/dispatch** to the main function around the group call
6. **Fix the opcode local** after the dispatch block closes (it gets set after spill)

### For block extraction functions (like func 1194)

1. **Identify large blocks** at target nesting depths, above a minimum size threshold
2. **Resolve ancestor conflicts** — if parent and child blocks both qualify, keep the parent only
3. **Extract each block** into a helper function, transforming:
   - Local indices: +2 offset
   - Internal branches: adjusted for new block structure
   - Enclosing-scope branches: converted to continuation codes
4. **Replace each block** in the main function with: spill → call → save result → reload → br_table dispatch on continuation code
5. **Add a spare local** to the main function for storing the continuation code before reload

## Recommended Next Steps

### Phase 1: Measure before optimizing (1-2 days)

1. Run the benchmark with **only func 482 split** (the known-good configuration)
2. Capture a detailed flame graph with async-profiler
3. Capture JIT compilation log (`-XX:+PrintCompilation`)
4. Identify the top 10 hottest functions and their binary sizes
5. Document which of those are >8KB and would benefit from C2

### Phase 2: Targeted splitting (based on Phase 1 data)

Only split functions that Phase 1 shows are both:
- Hot (>5% CPU time in flame graph)
- Large (>8KB binary, confirmed C1-only via PrintCompilation)

For each candidate:
- Estimate spill/reload cost: `locals_count × 6 × call_frequency`
- Estimate JIT benefit: compare C1 vs C2 compilation speed for that code pattern
- Only proceed if estimated benefit > estimated cost

### Phase 3: Reduce spill/reload overhead

If Phase 2 shows that more functions would benefit from splitting but are marginal due to spill/reload cost:
- Implement selective spilling (only live locals)
- Or: pass hot locals as WASM function parameters instead of through memory
- Or: explore Chicory-side solutions (method splitting at the Java bytecode level, avoiding WASM-level overhead entirely)

## Files

- `split_wasm.py` — splitting script (modify per-function configs, add new strategies)
- `perf-test/bench.sh` — benchmark script
- `perf-test/test` — jbang benchmark (50K insert + delete)
- `pom.xml` — Chicory plugin config (line 165: wasmFile path)
- `wasm-lib/libsqlite3.wasm` — original WASM input
- `wasm-lib/libsqlite3_split.wasm` — split WASM output

## Verification

1. `python3 split_wasm.py wasm-lib/libsqlite3.wasm wasm-lib/libsqlite3_split.wasm` — script succeeds
2. `wasm-validate wasm-lib/libsqlite3_split.wasm` — structural correctness
3. `mvn test` — all 432 tests pass
4. `perf-test/bench.sh` — execution time improves vs baseline
5. `-XX:+PrintCompilation` — confirm split pieces are C2-compiled
