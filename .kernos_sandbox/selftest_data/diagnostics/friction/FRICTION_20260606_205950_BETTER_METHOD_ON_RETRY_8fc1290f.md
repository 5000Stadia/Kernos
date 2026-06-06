# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-06T20:59:55.328485+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
The system tried `reminders` first, which failed, then retried with `remember`, which succeeded for the same request. This creates unnecessary latency and extra tool traffic for a known-working path. It matters because users see slower response times and the agent wastes effort on a method that is already known to be unreliable in this context. The likely fix is to enforce the working method as the default in code, so the request routes directly to `remember` instead of falling back through `reminders` first.

## Recommendation: STRUCTURAL_ENFORCE
`reminders` failed, then `remember` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: reminders
- succeeded: remember
- user message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.

## Context
User message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.
Space: space_2e080a73
Tools surfaced: 25
Tool calls: ['reminders', 'remember', 'reminders']
Merged count: 1
Reactive: True
