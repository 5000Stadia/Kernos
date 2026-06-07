# Friction Report: BETTER_METHOD_ON_RETRY
Generated: 2026-06-07T04:02:26.603011+00:00
Class: opportunity

**Confidence:** LOW (heuristic — may be a false positive)

## Description
The request first hit `memory.note_this`, failed, then succeeded on `note_this` for the same action. That means the system is taking a slower retry path to reach the method that actually works, instead of calling it directly.

Why it matters: it adds unnecessary latency, increases failure noise, and makes the tool surface harder to understand and maintain. It also risks inconsistent behavior if the fallback path changes.

Likely fix: make `note_this` the default routed method in code, or alias/redirect `memory.note_this` to it so the working path is used first and the retry path is not needed.

## Recommendation: STRUCTURAL_ENFORCE
`memory.note_this` failed, then `note_this` succeeded for the same request — consider making the working method the default here so it isn't reached for via the slower path.

## Evidence
- failed: memory.note_this
- succeeded: note_this
- user message: Hey, I want to see you actually use everything you've got. Go read your self-test checklist — it's in your docs at docs/V1-SELF-TEST.md — and then just work thr

## Context
User message: Hey, I want to see you actually use everything you've got. Go read your self-test checklist — it's in your docs at docs/V1-SELF-TEST.md — and then just work through it all yourself, for real: actually do each thing with your own tools, not describe it. Take them one at a time. Don't pass it off to another agent — I want to see you do it. When you're done, write up how it went in a file and tell me straight what worked, what was rough, and where you're at.
Space: space_53614448
Tools surfaced: 25
Tool calls: ['memory.note_this', 'note_this', 'note_this']
Merged count: 1
Reactive: True
