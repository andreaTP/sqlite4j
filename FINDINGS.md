# Phase 1 Findings: Profile-Guided Analysis

## Date: 2026-03-02
## Configuration: func 482 split into 5 groups (known-good baseline)

## Key Finding 1: JIT Compilation Dominates Runtime

**53.4% of total CPU time is spent in the C2 JIT compiler itself.**

| Category | Self-time % |
|----------|------------|
| JIT compiler overhead | 29.9% |
| WASM functions | 23.3% |
| Other | 29.0% |
| Memory access (getAddr/read/write) | 5.5% |
| Memory/Unsafe ops | 4.6% |
| Native memory ops | 3.8% |
| JIT adapters (I2C/C2I) | 1.1% |
| checkInterruption | 0.7% |
| Chicory dispatch | 0.6% |
| call_indirect | 0.3% |

This means our benchmark is **warmup-dominated**. Performance gains from enabling C2 will compound: better-compiled code → faster execution → less proportional JIT overhead.

## Key Finding 2: Hot Functions Stuck in Interpreter

9 functions have >8KB Java bytecode and **cannot be C2-compiled**. Of these, 5 are hot:

| Function | Bytecode | Self% | Total% | JIT Status | Action |
|----------|----------|-------|--------|------------|--------|
| func_180 | 42,545 B | 7.4% | 13.6% | NONE | **MUST SPLIT** |
| func_1194 | 28,350 B | 2.0% | 8.3% | NONE | **MUST SPLIT** |
| func_1260 | 21,116 B | 1.5% | 2.1% | NONE | SPLIT |
| func_1177 | 9,465 B | 3.4% | 6.2% | NONE | **MUST SPLIT** |
| func_1345 | 8,251 B | 1.3% | 1.7% | NONE | SPLIT |
| func_1435 | 8,168 B | 0.7% | 1.0% | NONE | CONSIDER |
| func_1146 | 16,968 B | 0.0% | 0.0% | NONE | SKIP |
| func_921 | 8,591 B | 0.0% | 0.1% | NONE | SKIP |

## Key Finding 3: Split Helper func_2708 is Too Large

func_2708 (Group 1 of func_482 split) has **12,045 bytes** of bytecode — exceeding the 8KB threshold. It is **never JIT compiled** (not even C1), leaving a gap in the VDBE main loop optimization.

The other 4 groups all achieved C2 compilation:
- func_2707 (Group 0): 6,262B → C2 ✓
- func_2708 (Group 1): 12,045B → **not compiled** ✗
- func_2709 (Group 2): 6,833B → C2 ✓
- func_2710 (Group 3): 5,618B → C2 ✓
- func_2711 (Group 4): 4,964B → C2 ✓

**Fix:** Increase func_482 from 5 to 6 groups, rebalancing handlers to keep all pieces <8KB.

## Key Finding 4: func_482 Split Helpers ARE C2-Compiled

The split strategy works: all under-8KB helpers for func_482 progressed C1 → C2:
- func_482 itself (the dispatcher): 6,630B → C2 ✓
- All under-8KB group functions: C2 ✓

This confirms the approach is sound — the issue in the previous experiment was likely:
1. func_2708 being too large (interpreter-only)
2. Splitting cold functions (func_1146, func_921) added overhead without benefit
3. Spill/reload cost for functions with many locals (func_180: 47 locals → ~282 instructions per dispatch)

## Top WASM Functions by Self-Time

| Rank | Function | Self% | Total% | Bytecode | C2? |
|------|----------|-------|--------|----------|-----|
| 1 | func_180 | 7.44% | 13.60% | 42,545 B | NO |
| 2 | func_1177 | 3.36% | 6.21% | 9,465 B | NO |
| 3 | func_1194 | 2.01% | 8.34% | 28,350 B | NO |
| 4 | func_1260 | 1.45% | 2.07% | 21,116 B | NO |
| 5 | func_1345 | 1.29% | 1.73% | 8,251 B | NO |
| 6 | func_482 | 1.18% | 24.01% | 6,630 B | YES |
| 7 | func_2708 | 0.67% | 0.90% | 12,045 B | NO |
| 8 | func_1435 | 0.67% | 1.01% | 8,168 B | NO |
| 9 | func_2484 | 0.56% | 0.45% | 1,792 B | YES |
| 10 | func_103 | 0.22% | 0.39% | 7,446 B | YES |

## Splitting Cost Estimates

| Function | Locals | Spill cost/dispatch | Strategy | Min groups |
|----------|--------|-------------------|----------|-----------|
| func_180 | 47 | ~282 instructions | br_table | 6 |
| func_1177 | 41 | ~246 instructions | br_table | 2 |
| func_1194 | 38 | ~228 instructions | br_table | 4 |
| func_1260 | 40 | ~240 instructions | br_table | 3 |
| func_1345 | 18 | ~108 instructions | br_table | 2 |
| func_1435 | 27 | ~162 instructions | br_table | 2 |
| func_482 | 34 | ~204 instructions | br_table | 6 (was 5) |

## Phase 2 Plan

Apply the >5% CPU threshold from PLAN.md:

### Must split (>5% total CPU time AND >8KB):
1. **Fix func_482**: 5 → 6 groups (fix func_2708 being >8KB)
2. **func_180**: 6 groups, br_table strategy (7.4% self, 13.6% total)
3. **func_1177**: 2 groups, br_table strategy (3.4% self, 6.2% total)
4. **func_1194**: 4 groups, br_table strategy (2.0% self, 8.3% total)

### Consider after measuring:
5. **func_1345**: 2 groups (only 18 locals = cheap spill/reload)
6. **func_1260**: 3 groups
7. **func_1435**: 2 groups
