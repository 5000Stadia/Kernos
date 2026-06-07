# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-07T03:00:05.752012+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
`workspace_code_execution.execute_code` failed, but the same request succeeded immediately with `execute_code`, which means the system is taking a slower, unreliable path before the working one. This matters because it adds retry latency, wastes tool calls, and can make execution appear flaky even when a valid method already exists.

Likely fix: make `execute_code` the default path for this workspace/task, and only fall back to `workspace_code_execution.execute_code` if the primary route is unavailable.

## Recommendation: SIMPLIFY
`workspace_code_execution.execute_code` failed, then `execute_code` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: workspace_code_execution.execute_code
- succeeded: execute_code
- user message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.

## Context
User message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.
Space: space_6c29713d
Tools surfaced: 25
Tool calls: ['workspace_code_execution.execute_code', 'execute_code']
Merged count: 1
Reactive: True
