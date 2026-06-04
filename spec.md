# spec.md

## Change

Make a small substrate-surface improvement with three durable artifacts:

1. Add `ensure_terminal_newline(text: str) -> str` to `kernos/utils.py`.
2. Add a docstring to the existing `ProviderRegistry.get()` method in `kernos/kernel/agents/providers.py` without changing its behavior.
3. Create `docs/notes/soak-03.md` containing exactly these three plain-text sentences:

This note was produced by Kernos's autonomous improvement loop on 2026-06-04.
It exists to test one tiny helper, one docstring, and one note in the same substrate pass.
The change is intentionally narrow.

## Acceptance Criteria

- AC1: `ensure_terminal_newline()` exists in `kernos/utils.py` and returns text with exactly one trailing newline, including when the input has no trailing newline or multiple trailing newlines.
- AC2: `ProviderRegistry.get()` has a clear docstring and continues to return the registered factory for known provider keys or `None` for absent keys.
- AC3: `docs/notes/soak-03.md` exists and contains exactly the three approved sentences above, with no orchestration status marker.
- AC4: The implementation includes one focused test that asserts both behavioral signal and durable substrate state in the same test.

## Out of Scope

- No changes to provider registration, provider construction, routing, gates, memory, or agent-flow behavior.
- No documentation index, catalog, or navigation updates.
- No broad formatting or refactoring.

## Risks

Low. The behavioral helper is additive, the provider change is docstring-only, and the note is a standalone documentation artifact.
