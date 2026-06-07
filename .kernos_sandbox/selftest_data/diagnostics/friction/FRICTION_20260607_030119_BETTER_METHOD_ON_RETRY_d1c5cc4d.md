# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-07T03:02:04.876907+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
`tool_registration.register_tool` failed and the system had to retry with `register_tool`, which succeeded for the same request. That means the preferred path is not the default, so the assistant is taking an avoidable slower/failing route before reaching the working method.

This matters because it adds latency, creates noisy failure telemetry, and makes tool behavior less reliable and harder to debug.

Likely fix: make `register_tool` the primary path in code and reserve `tool_registration.register_tool` only if it is genuinely required. If both exist, route directly to the working implementation instead of relying on retry.

## Recommendation: STRUCTURAL_ENFORCE
`tool_registration.register_tool` failed, then `register_tool` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: tool_registration.register_tool
- succeeded: register_tool
- user message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.

## Context
User message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.
Space: space_6c29713d
Tools surfaced: 25
Tool calls: ['tool_registration.register_tool', 'register_tool', 'register_tool']
Merged count: 1
Reactive: True
