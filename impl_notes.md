# Implementation Notes

## Changes

- Added `ensure_terminal_newline()` to `kernos/utils.py`; it returns text with exactly one trailing newline.
- Added a docstring to `ProviderRegistry.get()` without changing lookup behavior.
- Added `docs/notes/soak-03.md` with the three approved plain-text sentences.
- Added `tests/test_soak_03_surface.py`, a single substrate-fidelity test that checks runtime behavior and durable source/docs state together.
- Updated the existing loop-selftest dirty-scope allowlist for the approved helper/docstring/note/test surface.
- Replaced the corrupted `spec.md` progress-log text with the actual helper/docstring/soak-note acceptance-criteria spec.

## Acceptance Criteria Coverage

- AC1: The utility helper exists in `kernos/utils.py` and is exercised for both missing and extra newline cases.
- AC2: `ProviderRegistry.get()` now has a docstring while preserving registered-key and absent-key lookup behavior.
- AC3: `docs/notes/soak-03.md` exists and its exact three-sentence content is pinned by the test.
- AC4: The focused test uses the substrate-fidelity pattern by asserting behavioral signal and filesystem/source state in the same test.
- Reviewer finding: `spec.md` now contains the real approved acceptance criteria instead of the prior corrupted assistant-progress transcript.

## Verification

- Ran `python -m pytest tests/test_soak_03_surface.py tests/test_loop_selftest_note.py -q` (`3 passed`).

STATUS: GREEN
