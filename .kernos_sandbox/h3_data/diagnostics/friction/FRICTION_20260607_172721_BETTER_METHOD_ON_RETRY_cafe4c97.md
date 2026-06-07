# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-07T17:27:26.789400+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
The request “Keep going — next test in order, same rules.” first failed through `existing_self_test_plan` and only then succeeded via `run_self_test_suite`, adding avoidable retry latency and extra tool churn. This matters because the system is taking a slower fallback for a routine case, which makes behavior less efficient and more brittle. Make `run_self_test_suite` the default path for this request type, and keep the older plan only as a fallback if needed.

## Recommendation: SIMPLIFY
`existing_self_test_plan` failed, then `run_self_test_suite` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: existing_self_test_plan
- succeeded: run_self_test_suite
- user message: Keep going — next test in order, same rules.

## Context
User message: Keep going — next test in order, same rules.
Space: space_baadbf3d
Tools surfaced: 25
Tool calls: ['existing_self_test_plan', 'run_self_test_suite']
Merged count: 1
Reactive: True
