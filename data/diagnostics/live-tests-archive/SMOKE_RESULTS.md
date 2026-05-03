# Live Smoke Test Results

**Date:** 2026-04-08 01:54 UTC
**Result:** 11/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 7651ms
**Response:** It’s 6:53 PM.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 5806ms
**Response:** Tuesday, April 7, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 19502ms
**Response:** Doing well. ↵  ↵ How are you?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (2/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 1310ms
**Response:** Context dumped to data/diagnostics/context_2026-04-08T01-53-44.txt
- OK: DEPTH paragraph found in RULES block

### ✅ USER CONTEXT source tags + dedup (hotfix)
- OK: source tags present, no duplicates, no identity confusion

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 7898ms
**Response:** What city or neighborhood should I search in?
- OK: TOOL_SURFACING: tier=common surfaced=11 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=11 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 8876ms
**Response:** 2^100 = 1267650600228229401496703205376 ↵  ↵ I also tried `execute_code`, and the tool itself is misconfigured in this workspace: ↵ `python3: can't open file .../pow100.py`
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=4084
- assemble=1397ms route=2479ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- INFO: no shaping logs (may have no candidates)

