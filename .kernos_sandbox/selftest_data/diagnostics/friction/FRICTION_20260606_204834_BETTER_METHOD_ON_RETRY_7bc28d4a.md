# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-06T20:48:39.528760+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
The request hit a failing wrapper path first (`workspace_tool_registry.register_tool`), then succeeded on the direct method (`register_tool`) for the same action. This adds unnecessary retry latency, extra tool churn, and makes the system look less reliable even though a working path already exists. The likely fix is to route this operation directly to the working method by default in code, so the slower/failing wrapper is not used for this case.

## Recommendation: STRUCTURAL_ENFORCE
`workspace_tool_registry.register_tool` failed, then `register_tool` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: workspace_tool_registry.register_tool
- succeeded: register_tool
- user message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.

## Context
User message: Keep going — continue with the next test in order, same rules. When every test is done, write the results file and tell me you're finished.
Space: space_13363999
Tools surfaced: 24
Tool calls: ['workspace_tool_registry.register_tool', 'register_tool', 'register_tool_retry_descriptor_file_only']
Merged count: 1
Reactive: True
