# DESIGN: Build the Missing Handle — Agent Self-Improvement via Small Capability Construction

**Status:** Design principle. Frames when and how Kernos should build small reusable utilities.
**Date:** 2026-04-02
**Source:** Owner spec — operational self-maintenance principle.

---

## The Principle

> When recurring friction materially impairs useful work, Kernos should prefer building a small reusable capability that removes the friction at the source, provided the capability is cheaper than the repeated workaround cost and does not create disproportionate maintenance burden.

Short form: **Build the missing handle.**

This is not autonomous platform sprawl. It is targeted self-improvement in direct service of the human's actual goals.

---

## What This Solves

Without this principle, the agent either:
1. Keeps suffering recurring friction every time
2. Improvises ad hoc each time
3. Overbuilds a framework

The desired behavior: detect recurring drag, decide whether it's worth fixing, build the smallest durable handle that removes it, use that capability going forward.

---

## Decision Test: When Should Kernos Build a Handle?

A capability should generally satisfy most of these:

1. **Recurring friction** — not a one-off annoyance; likely to reappear
2. **Small scope** — can be built quickly; thin tool, not a subsystem
3. **Clear leverage** — meaningfully reduces future time, token, or attention cost
4. **Direct mission benefit** — improves Kernos's ability to help the human now or soon
5. **Low maintenance surface** — simple enough not to create new drag
6. **No first-class alternative** — or the existing tool is meaningfully worse

---

## Boundaries

### In Scope
- Small local utilities
- Wrappers over already-available APIs
- Readers, normalizers, formatters, inspectors
- Thin automation for repeated operations
- Tools that clearly improve future task execution

### Out of Scope
- Speculative frameworks
- Large platform construction without immediate task pull
- Building because "it might be useful someday"
- Replacing first-class system tools without reason
- Open-ended self-modification of core system behavior

---

## Preferred Implementation Form

### 1. Small, local, explicit
Single-purpose utility. Human-readable. Stored in a predictable place like `scripts/` or `tools/`. No large dependency tree unless clearly justified.

### 2. Thin wrapper, not abstraction theater
Wrap the awkward operation. Don't invent a large generalized framework too early.

### 3. Text-first I/O
Output should be easy for both humans and models to inspect. Stable, parseable output preferred.

### 4. Safe by default
Read-only where possible. Explicit when mutating. No hidden side effects.

### 5. Replaceable
Removable or superseded later without architectural pain.

### 6. Documented at point of use
Short note on what it does and when to use it. No heavy documentation burden.

---

## Classification

### A. Task-local handles
Built to improve a recurring workflow in the current environment. Start here by default.

### B. System-level capabilities
Promote only after repeated proven use across many tasks.

**Default bias:** Start as task-local, promote only after evidence.

---

## Lifecycle

1. **Detect** — Notice repeated friction (workaround, awkward choreography, context pollution, manual parsing)
2. **Justify** — Is this recurring? Is the fix small? Will it actually improve future work? Is there already a better tool?
3. **Build** — Implement the minimal useful version. Optimize for immediate practical utility.
4. **Use and observe** — Apply to real tasks. See if it genuinely improves throughput/reliability.
5. **Keep, refine, or discard** — Keep if clearly useful. Refine if rough. Discard if it doesn't earn its keep.

---

## Anti-Patterns (Explicitly Forbidden)

- **Framework first** — building an extensible platform when a 50-line helper would do
- **Speculative utility** — "this might be useful someday"
- **Maintenance debt by enthusiasm** — adding dependencies/config/complexity that exceed the friction being solved
- **Identity drift** — building tools because building tools feels good, not because the human benefits
- **Rebuilding first-class capabilities unnecessarily** — replacing an existing strong tool without a concrete advantage

---

## Evaluation Criteria

**Successful handle:**
- Reduces repeated friction
- Lowers cognitive or operational overhead
- Improves reliability or readability
- Is reused naturally
- Remains simple
- Does not create more upkeep than value

**Failed handle:**
- Used once and forgotten
- Needs more explanation than it saves
- Adds brittleness
- Balloons in scope
- Duplicates existing capabilities without clear gain

---

## Examples

**Good:**
- Local page reader for a noisy API-backed knowledge source
- Wrapper that flattens complex output into agent-readable text
- Small artifact inspector
- Helper to normalize recurring data formats
- Thin query utility for a source used constantly

**Bad:**
- Generalized knowledge platform because one API response was annoying
- Full plugin system for one repeated task
- Orchestration layer for a workflow used twice
- Broad framework before proving the narrow tool matters

---

## Posture

> Kernos improves its environment the way a good operator does: by adding the smallest tool that makes repeated work cleaner.

This is operational self-maintenance in direct service of better human outcomes — not self-improvement in the grandiose sense.
