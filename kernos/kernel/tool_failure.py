"""Typed tool-failure result — TOOL-ARG-REPAIR-V1 Phase 0.

Root cause (spec §1.4): a tool that RETURNS its failure (rather than
raising) was wrapped ``is_error=False`` at both live dispatch
boundaries, so semantic failures were invisible to the orchestration
layer — the plan-spine marked steps complete over them and nothing
retried.

``ToolFailure`` subclasses ``str`` deliberately:

  * Every legacy consumer that treats tool results as plain text
    (the agent tool loop, ``scheduler``'s trigger tool_call path,
    the consequence phase, json.dumps, ``isinstance(x, str)``
    checks, substring asserts in tests) keeps working byte-for-byte
    — the failure IS its message. This satisfies the spec's
    "plain-text fallback for legacy direct execute_tool consumers"
    note by construction.
  * Live dispatch boundaries (``LiveExecutor``,
    ``LiveIntegrationDispatcher``) detect ``isinstance(result,
    ToolFailure)`` and record ``is_error=True`` so
    ``StepDispatcher`` yields ``completed=False`` and the plan does
    NOT advance over the failure.

``pre_side_effect=True`` marks failures raised before the tool
mutated anything (validation rejections) — the only class of error
a future bounded auto-retry (Phase 3) is allowed to re-dispatch.
Errors are unsafe-to-retry unless explicitly tagged otherwise.
"""
from __future__ import annotations


class ToolFailure(str):
    """A tool's returned failure, typed so dispatch boundaries can see it.

    Behaves exactly like the failure-message string everywhere else.
    """

    code: str
    pre_side_effect: bool

    def __new__(
        cls,
        message: str,
        *,
        code: str = "tool_error",
        pre_side_effect: bool = False,
    ) -> "ToolFailure":
        obj = super().__new__(cls, message)
        obj.code = code
        obj.pre_side_effect = pre_side_effect
        return obj

    def __repr__(self) -> str:  # diagnostics: make the type visible in logs
        return (
            f"ToolFailure(code={self.code!r}, "
            f"pre_side_effect={self.pre_side_effect}, "
            f"message={str.__repr__(self)})"
        )
