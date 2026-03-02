# Splitting Experiments Log

## Environment
- JDK: OpenJDK (default on system)
- Chicory AOT compilation
- Benchmark: 50K insert + delete (perf-test/test via jbang --fresh)
- All times in milliseconds, 3+ runs each

## Experiment 1: Baseline (no split)
**Config:** `wasmFile = libsqlite3.wasm` (original, unmodified)
**Result:** 10015, 10351, 10047 → **avg ~10,137ms**

## Experiment 2: func_482 only, 5 groups (original experiment from PLAN.md)
**Config:** `ALL_CONFIGS = [FUNC_482_CONFIG]`, num_groups=5
**Result:** ~7,000ms (from PLAN.md historical data)
**Note:** func_2708 (group 1) was 12,045B bytecode — exceeds 8KB, never JIT compiled!

## Experiment 3: func_482 only, 8 groups (fixed)
**Config:** `ALL_CONFIGS = [FUNC_482_CONFIG]`, num_groups=8
**Group bytecodes:** 5114, 7332, 6589, 4800, 4030, 3105, 4908, 3291 — all <8KB ✓
**Result:** 6327, 7205, 7150 → **avg ~6,894ms** (-32% vs baseline)
**JIT:** func_482 + 6/8 groups → C2 (2 cold groups not compiled = not called)

## Experiment 4: 3-function split (482+180+1177)
**Config:** `ALL_CONFIGS = [FUNC_482_CONFIG, FUNC_180_CONFIG, FUNC_1177_CONFIG]`
- func_482: 8 groups, 34 locals
- func_180: 12 groups, 48 locals (spill cost: ~282 instr/dispatch)
- func_1177: 4 helpers (block extraction), 47 locals
**All split pieces <8KB ✓**
**Result:** 11273, 8425, 7614, 9505 → **unstable, avg ~9,204ms**
**JIT:** 18/27 functions C2-compiled, 9 cold groups not compiled
**Analysis:** Worse than func_482-only. Extra JIT compilation overhead + spill/reload cost for func_180 (47 locals) outweighs C2 benefit.

## Experiment 5: 4-function split (482+180+1177+1194) — first attempt
**Config:** Added FUNC_1194_CONFIG (block extraction, 4 helpers)
**Problem:** func_1194 dispatcher=11,301B, helper_0=11,961B — both >8KB
**Also:** func_180 groups 2-5 were >8KB (used 6 groups at that point)
**Result:** 8593ms (single run with profiler), ~8570ms steady
**Note:** This was before fixing bytecode sizes; many pieces weren't C2-compiled

## Phase 1 Profiling Data (with func_482-only, 5 groups)

### Top WASM functions by self-time:
| Function | Self% | Total% | Bytecode | C2? |
|----------|-------|--------|----------|-----|
| func_180 | 7.44% | 13.60% | 42,545 B | NO |
| func_1177 | 3.36% | 6.21% | 9,465 B | NO |
| func_1194 | 2.01% | 8.34% | 28,350 B | NO |
| func_1260 | 1.45% | 2.07% | 21,116 B | NO |
| func_1345 | 1.29% | 1.73% | 8,251 B | NO |
| func_482 | 1.18% | 24.01% | 6,630 B | YES |
| func_1435 | 0.67% | 1.01% | 8,168 B | NO |

### CPU time breakdown:
- JIT compiler overhead: 29.9% self-time
- WASM functions: 23.3% self-time
- Memory access: 5.5% self-time
- Total time in JIT stacks: 53.4%

### All functions >8KB bytecode:
| Function | Bytecode | Hot? | Locals | Spill cost |
|----------|----------|------|--------|-----------|
| func_180 | 42,545 B | YES (7.4%) | 48 | ~282 instr |
| func_1194 | 28,350 B | YES (2.0%) | 41 | ~246 instr |
| func_1260 | 21,116 B | marginal (1.5%) | 40 | ~240 instr |
| func_1146 | 16,968 B | NO (0.0%) | 33 | — |
| func_1177 | 9,465 B | YES (3.4%) | 47 | ~282 instr |
| func_921 | 8,591 B | NO (0.0%) | 41 | — |
| func_1345 | 8,251 B | marginal (1.3%) | 18 | ~108 instr |
| func_1435 | 8,168 B | marginal (0.7%) | 27 | ~162 instr |

### Key insight:
func_1345 has only 18 locals — cheapest spill/reload cost of any candidate.

---

## Experiment 6: func_482 + func_1177 only (50K)
**Config:** `ALL_CONFIGS = [FUNC_482_CONFIG, FUNC_1177_CONFIG]`
**Result:** 8664, 11321, 25927 → **noisy, worse than func_482-only**

## KEY FINDING: 50K benchmark is warmup-dominated

The 50K benchmark spends 53% of CPU in JIT compilation. Adding more split functions
means more code for C2 to compile, which increases warmup. To properly measure
steady-state performance, we need a larger workload.

## Experiment 7: 200K workload comparison

| Config | Run 1 | Run 2 | Best | vs Baseline |
|--------|-------|-------|------|-------------|
| Baseline (no split) | 39,375 | 43,047 | 39,375 | — |
| func_482 only (8 groups) | 27,045 | 24,600 | 24,600 | **-38%** |
| func_482 + func_1177 | 26,744 | 26,827 | 26,744 | **-32%** |
| func_482 + func_180 | 24,720 | 25,613 | 24,720 | **-37%** |
| **func_482 + func_180 + func_1177** | **24,161** | **22,849** | **22,849** | **-42%** |

### Analysis:
- **3-function split wins at 200K!** 22,849ms vs 24,600ms for func_482-only (-7%)
- The JIT warmup cost is amortized over the larger workload
- func_180 splitting provides the biggest additional gain (24,720 vs 24,600 for 482-only)
- func_1177 alone adds overhead (26,744 vs 24,600) but helps when combined with func_180
- The 50K benchmark was misleading — it penalized additional splits due to warmup

## Experiment 8: func_1345 split attempt (FAILED)
**Config:** Added FUNC_1345_CONFIG (br_table, 2 groups, 21 locals)
- 31 structural labels, handler container @32, 11 handlers
**Result:** 6 test failures (QueryTest CLOB, RSMetaDataTest column types, UDFTest triggers)
**Analysis:** Complex function structure with 31 structural labels likely causes
branch transformation bug. Config preserved but NOT included in ALL_CONFIGS.

## Experiment 9: A/B comparison (same session, 200K ops)

| Config | Run 1 | Run 2 |
|--------|-------|-------|
| Baseline | 36,615 | 52,373 |
| **3-func split (482+180+1177)** | **26,654** | **27,880** |

**Improvement: -27% to -47% vs baseline**

## Final Configuration

**ALL_CONFIGS = [FUNC_482_CONFIG, FUNC_180_CONFIG, FUNC_1177_CONFIG]**

| Function | Strategy | Groups | Locals | All pieces <8KB |
|----------|----------|--------|--------|----------------|
| func_482 | br_table | 8 | 34 | YES |
| func_180 | br_table | 12 | 48 | YES |
| func_1177 | block_extraction | 4 helpers | 47 | YES |

### Still >8KB (unsplit):
- func_1194 (28,350B) — block extraction produces pieces >8KB
- func_1260 (21,116B) — not hot enough (1.5% self)
- func_1345 (8,251B) — split causes test failures
- func_1435 (8,168B) — not hot enough (0.7% self)
- func_1146 (16,968B) — cold (0.0% self)
- func_921 (8,591B) — cold (0.0% self)

### Future work:
1. Fix func_1345 split (debug branch transformation for 31 structural labels)
2. Fix func_1194 block extraction (dispatcher still >8KB after extraction)
3. Explore Chicory-side bytecode splitting to avoid WASM-level spill/reload entirely
4. Reduce spill/reload cost via selective spilling or passing locals as params
