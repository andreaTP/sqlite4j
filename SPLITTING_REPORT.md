# WASM Function Splitting for sqlite4j: Report

## Problem

sqlite4j runs SQLite compiled to WebAssembly, executed via Chicory AOT which translates WASM functions into Java methods. HotSpot's C2 JIT compiler refuses to optimize any Java method larger than ~8KB of bytecode, falling back to the interpreter. Several critical SQLite functions exceed this threshold, leaving them unoptimized.

## Approach

We split large WASM functions into smaller pieces using a Python script (`split_wasm.py`) that operates on the WAT (text) representation. Two strategies:

- **br_table dispatch**: For functions with a switch-style opcode dispatch loop (like the VDBE main loop). The function is split into a thin dispatcher that calls group functions, each handling a subset of opcodes.
- **Block extraction**: For functions with large nested blocks. Individual blocks are extracted into helper functions.

Both strategies use a spill/reload mechanism: before calling a helper, all local variables are saved to linear memory; after the call, they're restored. This costs ~6 WASM instructions per local variable per call.

## What We Did

### Phase 1: Profiling

We profiled the benchmark (50K insert + delete) using async-profiler and `-XX:+PrintCompilation` to identify:

1. **Which functions are hot** (CPU time in flame graph)
2. **Which functions are >8KB** (bytecode size from class file parsing)
3. **Which functions are JIT-compiled** (C1/C2 tier from JIT log)

Key finding: **9 functions exceed 8KB bytecode. Of those, 5 are hot and running entirely in the interpreter** — not even C1-compiled.

| Function | Bytecode | Self-time | Role |
|----------|----------|-----------|------|
| func_482 | 6,630 B* | 1.2% self, 24% total | VDBE main loop (already split) |
| func_180 | 42,545 B | 7.4% self, 13.6% total | Expression evaluator |
| func_1177 | 9,465 B | 3.4% self, 6.2% total | Query planner helper |
| func_1194 | 28,350 B | 2.0% self, 8.3% total | Query optimizer |
| func_1345 | 8,251 B | 1.3% self, 1.7% total | Code generator |

*func_482 was already split in a prior experiment, so its bytecode was under 8KB.

We also discovered that **53% of benchmark CPU time was spent inside the C2 JIT compiler itself**, meaning the 50K-operation benchmark was warmup-dominated and not representative of steady-state performance.

### Phase 2: Targeted Splitting

We applied splitting to 3 functions:

| Function | Strategy | Groups/Helpers | Locals | Max piece bytecode |
|----------|----------|---------------|--------|-------------------|
| func_482 | br_table | 8 groups | 34 | 7,332 B |
| func_180 | br_table | 12 groups | 48 | 7,698 B |
| func_1177 | block_extraction | 4 helpers | 47 | 4,307 B |

All pieces are under 8KB. The dispatchers and most helpers achieve C2 compilation. Cold helpers (for opcodes not exercised by the benchmark) remain uncompiled, which is expected and harmless.

### What Didn't Work

- **func_1345**: Split produces test failures (6 errors). The function has 31 structural labels — likely a branch transformation bug in the splitter.
- **func_1194**: Block extraction produces helpers and a dispatcher that both exceed 8KB. The extraction removes blocks but the remaining code plus spill/reload overhead still exceeds the threshold.
- **More groups doesn't always help**: Our first attempt used too few groups for func_180 (6 groups → 4 pieces exceeded 8KB). We iterated from 6 → 10 → 12 groups before all pieces fit.

## Results

We benchmarked with a 200K-operation workload to amortize JIT warmup:

| Configuration | Time (best of 2) | vs Baseline |
|--------------|-------------------|-------------|
| Baseline (no split) | 36,615 ms | — |
| func_482 only (8 groups) | 24,600 ms | **-33%** |
| func_482 + func_180 + func_1177 | 22,849 ms | **-38%** |

With the shorter 50K benchmark, adding splits beyond func_482 appeared to hurt performance because JIT warmup dominated. The 200K benchmark reveals that at steady state, splitting all 3 functions is the best configuration.

## Files

| File | Purpose |
|------|---------|
| `split_wasm.py` | Splitting script with per-function configs |
| `wasm-lib/libsqlite3.wasm` | Original WASM input (868 KB) |
| `wasm-lib/libsqlite3_split.wasm` | Split output (906 KB, +4.3%) |
| `pom.xml` (line 165) | Points Chicory AOT at the split WASM |
| `perf-test/test` | 50K-op benchmark |
| `perf-test/test_200k` | 200K-op benchmark |
| `EXPERIMENTS.md` | Detailed experiment log with all timing data |

## Remaining Opportunities

1. **Fix func_1345 split** — debug the branch transformation for functions with many structural labels (18 locals = very cheap spill/reload, so this would be profitable if it worked).
2. **Fix func_1194** — the block extraction strategy needs to produce smaller pieces, possibly by extracting more/smaller blocks or using a different strategy entirely.
3. **Chicory-side splitting** — splitting at the Java bytecode level rather than WASM level would eliminate spill/reload overhead entirely, since Java locals don't need to be marshalled through memory.
4. **Selective spilling** — only spill locals that are live at the call site, rather than all locals. Would reduce per-dispatch overhead significantly for functions with many locals.
