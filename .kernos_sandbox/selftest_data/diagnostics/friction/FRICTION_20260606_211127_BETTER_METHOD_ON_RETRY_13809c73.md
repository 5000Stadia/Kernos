# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-06T21:11:32.664749+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
`code_execution.execute_code` failed, then `execute_code` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Recommendation: SIMPLIFY
`code_execution.execute_code` failed, then `execute_code` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: code_execution.execute_code
- succeeded: execute_code
- user message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.

## Context
User message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.
Space: space_2e080a73
Tools surfaced: 25
Tool calls: ['code_execution.execute_code', 'execute_code']
Merged count: 1
Reactive: True
