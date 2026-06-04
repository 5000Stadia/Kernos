# Implementation Notes

## Changes

- Updated `docs/notes/loop-selftest.md` to the accepted note shape: required heading, exactly one body paragraph, `June 3, 2026`, the runtime-neutral operator-approved documentation-only claim, and a body under 60 words.
- Replaced the brittle pytest text pin with invariant checks for the reader-visible note behavior and durable filesystem state in the same test.
- Added a dirty-worktree scope assertion so this loop-selftest change stays confined to `docs/notes/loop-selftest.md`, `tests/test_loop_selftest_note.py`, `impl_notes.md`, and optional `spec.md`.
- Added marker-confinement coverage so orchestration markers stay out of the note and optional spec artifact, with the required marker only at the end of this file.

## Acceptance Criteria Coverage

- AC1: `docs/notes/loop-selftest.md` exists.
- AC2: The note starts with `# Loop self-test note` and has exactly one body paragraph.
- AC3: The body paragraph includes `June 3, 2026`.
- AC4: The body paragraph states that the autonomous-improvement loop carried an operator-approved documentation-only change into the worktree without altering runtime behavior.
- AC5: The pytest enforces the body paragraph word count is no more than 60 words.
- AC6: `test_loop_selftest_note_behavior_and_substrate_state` checks behavioral signal and exact filesystem substrate state together.
- AC7: `test_loop_selftest_dirty_scope_and_marker_confinement` enforces the approved dirty-file scope.
- AC8: The same test confines orchestration markers to the required terminator in this implementation note.

## Prior Findings Addressed

- Note heading/date/wording/word-count invariants were missing: fixed in the note and enforced by pytest.
- Pytest pinned non-compliant text: replaced with AC-level invariant assertions over the compliant note.
- Pytest did not enforce dirty-file scope: added a `git status --porcelain --untracked-files=all` scope assertion for the allowed loop-selftest artifacts.

## Verification

- Ran `python -m pytest tests/test_loop_selftest_note.py` (`2 passed`).

STATUS: GREEN
