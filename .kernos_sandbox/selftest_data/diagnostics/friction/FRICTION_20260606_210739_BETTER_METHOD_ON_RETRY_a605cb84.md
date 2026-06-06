# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-06T21:07:42.376605+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
The request hit `workspace_tool_registration.register_tool` first, failed, and then succeeded via `register_tool` on retry. This adds avoidable latency, extra tool traffic, and failure noise for the same action. The likely fix is to make the working `register_tool` path the default in code and remove or bypass the slower/fragile fallback path instead of relying on retry behavior.

## Recommendation: STRUCTURAL_ENFORCE
`workspace_tool_registration.register_tool` failed, then `register_tool` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: workspace_tool_registration.register_tool
- succeeded: register_tool
- user message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.

## Context
User message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.
Space: space_2e080a73
Tools surfaced: 25
Tool calls: ['register_tool', 'register_tool', 'register_tool', 'workspace_tool_registration.register_tool', 'register_tool', 'register_tool', 'register_tool', 'register_tool']
Merged count: 1
Reactive: True
