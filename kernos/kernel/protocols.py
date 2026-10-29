"""Explicit boundary contracts for testability and maintainability.

Defines the handler↔reasoning interface so neither side reaches into
the other's private state. These are documentation + test contracts,
not engine-swappability abstractions.
"""
from typing import Protocol, runtime_checkable


@runtime_checkable
class HandlerProtocol(Protocol):
    """What ReasoningService needs from the handler."""

    async def send_outbound(
        self, instance_id: str, member_id: str, channel: str, text: str,
    ) -> bool: ...

    async def read_log_text(
        self, instance_id: str, space_id: str, log_number: int,
        member_id: str = "",
    ) -> str | None: ...


@runtime_checkable
class ReasoningProtocol(Protocol):
    """What the handler needs from ReasoningService.

    Narrow — only methods handler.py actually calls.
    """

    # Core reasoning
    async def execute_tool(
        self, tool_name: str, tool_input: dict, request: "ReasoningRequest",
    ) -> str: ...

    async def complete_simple(
        self, system_prompt: str, user_content: str,
        max_tokens: int = 1024, prefer_cheap: bool = False,
        output_schema: dict | None = None,
        chain: str | None = None,
    ) -> str: ...

    # Tool state
    def get_loaded_tools(self, space_id: str) -> set[str]: ...
    def clear_loaded_tools(self, space_id: str) -> None: ...

    # Confirmation / pending state (returns copies, not mutable internals)
    def get_pending_actions(self, instance_id: str) -> list | None: ...
    def clear_pending_actions(self, instance_id: str) -> None: ...
    def get_conflict_raised(self) -> bool: ...
    def reset_conflict_raised(self) -> None: ...

    # Tool config change tracking
    def get_tools_changed(self) -> bool: ...
    def reset_tools_changed(self) -> None: ...

    # Housekeeping
    def cleanup_expired_authorizations(self, instance_id: str) -> None: ...

    # Model info
    @property
    def main_model(self) -> str: ...
