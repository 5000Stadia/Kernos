# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-06T21:21:10.204683+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
`member_management.manage_members` failed on the first attempt, then `manage_members` succeeded for the same request, which indicates we’re routing to a slower or less reliable path before the known-good one. This matters because it adds avoidable latency, increases retry churn, and makes tool selection look inconsistent. The likely fix is to simplify dispatch so the working method is the default path for this operation, instead of only being reached after failure.

## Recommendation: SIMPLIFY
`member_management.manage_members` failed, then `manage_members` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: member_management.manage_members
- succeeded: manage_members
- user message: Open your self-test checklist at docs/V1-SELF-TEST.md and run tests 12 through 17 — members, relationships/disclosure, the dispatch-gate self-review, the improv

## Context
User message: Open your self-test checklist at docs/V1-SELF-TEST.md and run tests 12 through 17 — members, relationships/disclosure, the dispatch-gate self-review, the improve_kernos vs personal-tool boundary, one real consult to an external agent, and the admin/introspection one. Same rules: actually use your own tools for each, don't delegate the work, take them one at a time.
Space: space_973224d6
Tools surfaced: 25
Tool calls: ['member_management.manage_members', 'manage_members']
Merged count: 1
Reactive: True
