# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-07T02:51:18.045659+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
The request hit `reminders.manage_schedule` first, failed, then retried successfully through `manage_schedule` for the same action. This creates avoidable latency and extra tool churn, and it makes the system look flaky even though a working method already exists. Make the successful method the canonical default in code so the assistant routes there directly instead of relying on a slower fallback path.

## Recommendation: STRUCTURAL_ENFORCE
`reminders.manage_schedule` failed, then `manage_schedule` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: reminders.manage_schedule
- succeeded: manage_schedule
- user message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.

## Context
User message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.
Space: space_6c29713d
Tools surfaced: 25
Tool calls: ['reminders.manage_schedule', 'manage_schedule', 'manage_schedule']
Merged count: 1
Reactive: True
