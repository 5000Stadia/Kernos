# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-06T21:22:05.712651+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
`member_management.manage_members` failed, but the same request succeeded immediately with `manage_members`, which means the system is trying a slower or brittle path before the working one. This adds latency, creates avoidable failure noise, and makes the tool surface harder to reason about. The likely fix is to make `manage_members` the default entry point and stop routing through the failing `member_management.manage_members` path unless there is a real distinction.

## Recommendation: SIMPLIFY
`member_management.manage_members` failed, then `manage_members` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: member_management.manage_members
- succeeded: manage_members
- user message: Keep going — next test in order, same rules. When 12-17 are all done, tell me you're finished.

## Context
User message: Keep going — next test in order, same rules. When 12-17 are all done, tell me you're finished.
Space: space_973224d6
Tools surfaced: 25
Tool calls: ['member_management.manage_members', 'manage_members']
Merged count: 1
Reactive: True
