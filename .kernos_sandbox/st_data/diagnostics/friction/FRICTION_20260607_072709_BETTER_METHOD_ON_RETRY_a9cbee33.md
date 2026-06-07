# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-07T07:27:13.646083+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
The request was routed through `memory.note_this`, which failed, and only then retried successfully with `note_this`. This means the system is using a slower, less reliable fallback path for a method that already works.

Why it matters: it adds avoidable latency, creates noisy failure signals, and can make the same operation look flaky even when a valid implementation exists.

Likely fix: enforce the working method as the default in code, and route directly to `note_this` instead of relying on retry to discover it.

## Recommendation: STRUCTURAL_ENFORCE
`memory.note_this` failed, then `note_this` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: memory.note_this
- succeeded: note_this
- user message: Hey, I want to see you actually use everything you've got. Go read your self-test checklist — it's in your docs at docs/V1-SELF-TEST.md — and then just work thr

## Context
User message: Hey, I want to see you actually use everything you've got. Go read your self-test checklist — it's in your docs at docs/V1-SELF-TEST.md — and then just work through it all yourself, for real: actually do each thing with your own tools, not describe it. Take them one at a time. Don't pass it off to another agent — I want to see you do it. When you're done, write up how it went in a file and tell me straight what worked, what was rough, and where you're at.
Space: space_a3b90c39
Tools surfaced: 25
Tool calls: ['memory.note_this', 'note_this', 'note_this']
Merged count: 1
Reactive: True
