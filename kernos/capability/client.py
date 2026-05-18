from __future__ import annotations

import asyncio
import logging
import subprocess
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if TYPE_CHECKING:
    from kernos.kernel.events import EventStream


@dataclass
class AuthCommand:
    """Shell command to run when a server needs OAuth re-authentication."""
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    probe_tool: str = ""  # Tool to call at boot to verify auth (e.g. "get-current-time")

logger = logging.getLogger(__name__)

# Per-tool timeout defaults
TOOL_TIMEOUT_SECONDS: float = 30  # Default for most tools
TOOL_TIMEOUT_OVERRIDES: dict[str, float] = {
    "goto": 45,              # Browser navigation can be slow
    "markdown": 45,          # Page rendering can be slow
    "brave_web_search": 15,  # Search should be fast
    "get-current-time": 5,   # Should be instant
}

# Retry policy for transient failures
MAX_RETRIES = 1          # One retry only
RETRY_BACKOFF_S = 1.5    # Wait 1.5 seconds before retry

# Error-in-success detection: tool returns error text as "successful" output
ERROR_IN_RESULT_PATTERNS = [
    "error: rate limit",
    "error: too many requests",
    "error: 429",
    "error: 503",
    "error: service unavailable",
    "error: temporarily unavailable",
]


class MCPClientManager:
    """Manages connections to MCP servers and exposes their tools.

    Lifecycle: create once at app startup, call connect_all(), use for the
    lifetime of the process, call disconnect_all() on shutdown.
    """

    # Per-server rate limiting for burst protection during plan execution.
    # Servers not listed here have no limit.
    # Format: (max_concurrent, min_delay_seconds_between_calls)
    # Semaphore(1) + delay = fully sequential with spacing.
    _RATE_LIMITS: dict[str, tuple[int, float]] = {
        "brave-search": (1, 1.0),  # sequential, 1s between each call
    }

    def __init__(self, events: EventStream | None = None) -> None:
        self._servers: dict[str, StdioServerParameters] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._tool_to_session: dict[str, str] = {}  # tool_name → server_name
        self._tools: list[dict] = []
        self._exit_stack = AsyncExitStack()
        self._events = events
        self._runtime_stacks: dict[str, AsyncExitStack] = {}
        self._auth_commands: dict[str, AuthCommand] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._last_call_time: dict[str, float] = {}  # server_name → monotonic timestamp

    def register_server(self, name: str, params: StdioServerParameters) -> None:
        """Register an MCP server configuration. Does not connect."""
        self._servers[name] = params
        logger.info("Registered MCP server: %s", name)

    def register_auth_command(self, server_name: str, auth: AuthCommand) -> None:
        """Register an auth command to run when a server returns invalid_grant."""
        self._auth_commands[server_name] = auth

    async def connect_all(self) -> None:
        """Connect to all registered servers and discover their tools."""
        from kernos.kernel.event_types import EventType
        from kernos.kernel.events import emit_event

        for name, params in self._servers.items():
            try:
                read, write = await self._exit_stack.enter_async_context(
                    stdio_client(params)
                )
                session = await self._exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                self._sessions[name] = session

                result = await session.list_tools()
                tool_names = []
                for tool in result.tools:
                    self._tool_to_session[tool.name] = name
                    self._tools.append(
                        {
                            "name": tool.name,
                            "description": tool.description or "",
                            "input_schema": tool.inputSchema,
                        }
                    )
                    tool_names.append(tool.name)
                    logger.info("Discovered tool: %s (server: %s)", tool.name, name)

                logger.info(
                    "Connected to MCP server %s — %d tools discovered",
                    name,
                    len(result.tools),
                )
                if self._events:
                    try:
                        await emit_event(
                            self._events,
                            EventType.CAPABILITY_CONNECTED,
                            "system",
                            "capability_manager",
                            payload={
                                "server_name": name,
                                "tool_count": len(tool_names),
                                "tool_names": tool_names,
                                "error": None,
                            },
                        )
                    except Exception as exc:
                        logger.warning("Failed to emit capability.connected: %s", exc)

            except Exception as exc:
                logger.error(
                    "Failed to connect to MCP server %s: %s",
                    name,
                    exc,
                    exc_info=True,
                )
                if self._events:
                    try:
                        from kernos.kernel.event_types import EventType
                        from kernos.kernel.events import emit_event

                        await emit_event(
                            self._events,
                            EventType.CAPABILITY_ERROR,
                            "system",
                            "capability_manager",
                            payload={
                                "server_name": name,
                                "tool_count": 0,
                                "tool_names": [],
                                "error": str(exc),
                            },
                        )
                    except Exception as emit_exc:
                        logger.warning("Failed to emit capability.error: %s", emit_exc)

        # Probe auth on servers that have a probe_tool configured
        for name in list(self._sessions.keys()):
            auth = self._auth_commands.get(name)
            if not auth or not auth.probe_tool:
                continue
            probe_result = await self.call_tool(auth.probe_tool, {})
            if "invalid_grant" in probe_result.lower():
                logger.warning(
                    "TOOL_AUTH_PROBE: server=%s probe returned invalid_grant, triggering re-auth",
                    name,
                )
                reconnected = await self._reconnect_server(name)
                if not reconnected:
                    logger.error("TOOL_AUTH_PROBE: server=%s re-auth failed at boot", name)
            else:
                logger.info("TOOL_AUTH_PROBE: server=%s auth verified OK", name)

    async def connect_one(self, server_name: str) -> bool:
        """Connect a single MCP server at runtime. Returns True on success."""
        if server_name not in self._servers:
            return False
        if server_name in self._sessions:
            return True  # Already connected

        params = self._servers[server_name]
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[server_name] = session
            self._runtime_stacks[server_name] = stack

            result = await session.list_tools()
            tool_names = []
            for tool in result.tools:
                self._tool_to_session[tool.name] = server_name
                self._tools.append(
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema,
                    }
                )
                tool_names.append(tool.name)
                logger.info("Discovered tool: %s (server: %s)", tool.name, server_name)

            logger.info(
                "Connected to MCP server %s — %d tools discovered",
                server_name,
                len(result.tools),
            )
            from kernos.kernel.event_types import EventType
            from kernos.kernel.events import emit_event
            if self._events:
                try:
                    await emit_event(
                        self._events,
                        EventType.CAPABILITY_CONNECTED,
                        "system",
                        "capability_manager",
                        payload={
                            "server_name": server_name,
                            "tool_count": len(tool_names),
                            "tool_names": tool_names,
                            "error": None,
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to emit capability.connected: %s", exc)
            return True
        except Exception as exc:
            logger.warning("Failed to connect %s: %s", server_name, exc)
            try:
                await stack.aclose()
            except Exception:
                pass
            return False

    async def disconnect_one(self, server_name: str) -> bool:
        """Disconnect a single MCP server at runtime. Returns True if it was connected."""
        if server_name not in self._sessions:
            return False

        # Remove tools for this server
        self._tools = [
            t for t in self._tools
            if self._tool_to_session.get(t["name"]) != server_name
        ]
        self._tool_to_session = {
            k: v for k, v in self._tool_to_session.items() if v != server_name
        }
        del self._sessions[server_name]

        # Close runtime stack if one exists (servers connected via connect_one)
        if server_name in self._runtime_stacks:
            try:
                await self._runtime_stacks[server_name].aclose()
            except Exception:
                pass
            del self._runtime_stacks[server_name]

        if self._events:
            try:
                from kernos.kernel.event_types import EventType
                from kernos.kernel.events import emit_event
                await emit_event(
                    self._events,
                    EventType.CAPABILITY_DISCONNECTED,
                    "system",
                    "capability_manager",
                    payload={
                        "server_name": server_name,
                        "tool_count": 0,
                        "tool_names": [],
                        "error": None,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit capability.disconnected: %s", exc)

        return True

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers and clean up subprocesses."""
        if self._events:
            from kernos.kernel.event_types import EventType
            from kernos.kernel.events import emit_event

            for name in list(self._sessions.keys()):
                tools = [t for t, s in self._tool_to_session.items() if s == name]
                try:
                    await emit_event(
                        self._events,
                        EventType.CAPABILITY_DISCONNECTED,
                        "system",
                        "capability_manager",
                        payload={
                            "server_name": name,
                            "tool_count": len(tools),
                            "tool_names": tools,
                            "error": None,
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to emit capability.disconnected: %s", exc)

        for name, stack in list(self._runtime_stacks.items()):
            try:
                await stack.aclose()
            except Exception:
                pass
        self._runtime_stacks.clear()

        await self._exit_stack.aclose()
        logger.info("Disconnected from all MCP servers")

    def get_tools(self) -> list[dict]:
        """Return all available tools in Anthropic API format."""
        return list(self._tools)

    def get_tool_definitions(self) -> dict[str, list[dict]]:
        """Return tool definitions grouped by server name.

        Returns: {"google-calendar": [{"name": "get-events", ...}, ...]}
        """
        result: dict[str, list[dict]] = {}
        for tool in self._tools:
            server = self._tool_to_session.get(tool["name"], "unknown")
            result.setdefault(server, []).append(tool)
        return result

    @staticmethod
    def _is_transient(error_msg: str, exc: Exception | None = None) -> bool:
        """Distinguish transport failure from valid tool error.

        Transport failures are retryable. Application errors are not.
        """
        if isinstance(exc, asyncio.TimeoutError):
            return True
        if isinstance(exc, (ConnectionError, ConnectionResetError,
                            ConnectionRefusedError)):
            return True

        lower = error_msg.lower()

        # Explicitly NOT transient
        if any(s in lower for s in [
            "not found", "not available", "validation",
            "permission", "unauthorized", "forbidden",
            "invalid", "auth",
        ]):
            return False

        # Transient indicators
        return any(s in lower for s in [
            "503", "429", "rate limit", "timeout",
            "temporarily unavailable", "connection reset",
            "connection refused", "service unavailable",
        ])

    @staticmethod
    def _is_error_in_result(result_text: str) -> bool:
        """Detect error responses returned as successful tool output.

        Short results (<500 chars): check contains.
        Long results: only match if error is at the very start.
        """
        lower = result_text.strip().lower()
        if len(lower) < 500:
            return any(p in lower for p in ERROR_IN_RESULT_PATTERNS)
        return any(lower.startswith(p) for p in ERROR_IN_RESULT_PATTERNS)

    async def _run_auth_command(self, server_name: str) -> bool:
        """Run the registered auth command for a server (OAuth re-auth flow).

        Spawns the auth process (which typically opens a browser for user consent),
        waits up to AUTH_TIMEOUT_S for completion, then returns success/failure.
        """
        auth = self._auth_commands.get(server_name)
        if not auth:
            logger.warning("TOOL_AUTH_REAUTH: no auth command registered for %s", server_name)
            return False

        import os
        env = {**os.environ, **auth.env}
        cmd = [auth.command] + auth.args
        logger.info("TOOL_AUTH_REAUTH: running %s for server=%s", cmd, server_name)

        try:
            # Don't capture stdout/stderr — the auth command may print a URL
            # the user needs to see, or open a browser automatically.
            proc = await asyncio.create_subprocess_exec(*cmd, env=env)
            await asyncio.wait_for(proc.wait(), timeout=120)
            if proc.returncode == 0:
                logger.info("TOOL_AUTH_REAUTH: server=%s auth succeeded", server_name)
                return True
            else:
                logger.warning(
                    "TOOL_AUTH_REAUTH: server=%s auth failed (rc=%d)",
                    server_name, proc.returncode,
                )
                return False
        except asyncio.TimeoutError:
            logger.warning("TOOL_AUTH_REAUTH: server=%s auth timed out after 120s", server_name)
            try:
                proc.terminate()
            except Exception:
                pass
            # ZOMBIE-CHILD-PROCESS-REAP-V1 (2026-05-17): wait briefly
            # for the OS to deliver SIGTERM and reap the child.
            # Without this await, terminate() returns immediately
            # but the child sits as a zombie until parent exits —
            # observed accumulating at ~18/day on long-running bots.
            # 5s ceiling so a stuck child doesn't block the bot; if
            # SIGTERM doesn't take, escalate to SIGKILL. The periodic
            # reaper loop in server.py catches anything this misses.
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except Exception:
                    pass
            return False
        except Exception as exc:
            logger.warning("TOOL_AUTH_REAUTH: server=%s auth error: %s", server_name, exc)
            return False

    async def _reconnect_server(self, server_name: str) -> bool:
        """Re-authenticate (if auth command exists) then reconnect an MCP server.

        Used when invalid_grant is detected — the refresh token is dead,
        so we need to run the OAuth flow before restarting the server.
        """
        try:
            # Run auth flow first to get fresh tokens on disk
            if server_name in self._auth_commands:
                auth_ok = await self._run_auth_command(server_name)
                if not auth_ok:
                    logger.warning("TOOL_AUTH_RECONNECT: server=%s re-auth failed, skipping reconnect", server_name)
                    return False

            await self.disconnect_one(server_name)
            success = await self.connect_one(server_name)
            if success:
                logger.info("TOOL_AUTH_RECONNECT: server=%s reconnected successfully", server_name)
            else:
                logger.warning("TOOL_AUTH_RECONNECT: server=%s reconnect failed", server_name)
            return success
        except Exception as exc:
            logger.warning("TOOL_AUTH_RECONNECT: server=%s error: %s", server_name, exc)
            return False

    def _get_timeout(self, tool_name: str) -> float:
        """Return the timeout for a given tool."""
        return TOOL_TIMEOUT_OVERRIDES.get(tool_name, TOOL_TIMEOUT_SECONDS)

    async def call_tool(self, tool_name: str, tool_args: dict,
                        timeout: float | None = None) -> str:
        """Call an MCP tool by name. Returns result text or an error string.

        Never raises — all errors are returned as descriptive strings.
        Retries once on transient transport failures with 1.5s backoff.
        """
        server_name = self._tool_to_session.get(tool_name)
        if server_name is None:
            logger.error("Tool not found: %s", tool_name)
            return f"Tool error: '{tool_name}' is not available."

        session = self._sessions.get(server_name)
        if session is None:
            logger.error("Session not found for server: %s", server_name)
            return f"Tool error: server '{server_name}' is not connected."

        effective_timeout = timeout or self._get_timeout(tool_name)

        # Per-server rate limiting (semaphore + inter-call delay)
        _rate = self._RATE_LIMITS.get(server_name)
        if _rate:
            _max_concurrent, _delay = _rate
            if server_name not in self._semaphores:
                self._semaphores[server_name] = asyncio.Semaphore(_max_concurrent)

        for attempt in range(1 + MAX_RETRIES):
            exc_ref: Exception | None = None
            error_msg = ""
            try:
                # Rate-limited call: serialize through semaphore + enforce delay
                if _rate:
                    _sem = self._semaphores[server_name]
                    async with _sem:
                        # Wait until min_delay has elapsed since last call finished
                        _now = asyncio.get_event_loop().time()
                        _last = self._last_call_time.get(server_name, 0)
                        _wait = _rate[1] - (_now - _last)
                        if _wait > 0:
                            await asyncio.sleep(_wait)
                        result = await asyncio.wait_for(
                            session.call_tool(tool_name, tool_args),
                            timeout=effective_timeout,
                        )
                        # Record AFTER call completes — next caller waits from this point
                        self._last_call_time[server_name] = asyncio.get_event_loop().time()
                else:
                    # Timeout wraps ONLY the actual MCP session call
                    result = await asyncio.wait_for(
                        session.call_tool(tool_name, tool_args),
                        timeout=effective_timeout,
                    )
                # Formatting/processing happens OUTSIDE timeout
                parts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        parts.append(content.text)
                    else:
                        parts.append(str(content))
                formatted = "\n".join(parts) if parts else "(empty result)"

                # Check for auth failure requiring server reconnect
                if "invalid_grant" in formatted.lower():
                    logger.warning(
                        "TOOL_AUTH_RECONNECT: tool=%s — invalid_grant detected, reconnecting server",
                        tool_name,
                    )
                    reconnected = await self._reconnect_server(server_name)
                    if reconnected and attempt < MAX_RETRIES:
                        # Update session reference for retry
                        session = self._sessions.get(server_name)
                        if session:
                            logger.info(
                                "TOOL_AUTH_RECONNECT: server=%s reconnected, retrying",
                                server_name,
                            )
                            await asyncio.sleep(RETRY_BACKOFF_S)
                            continue
                    # Reconnect failed or retries exhausted
                    return f"Tool error: {formatted}"

                # Check for error-shaped successful results
                if self._is_error_in_result(formatted):
                    logger.warning(
                        "TOOL_ERROR_IN_RESULT: tool=%s result=%s",
                        tool_name, formatted[:200],
                    )
                    error_msg = f"Tool error: {formatted}"
                    exc_ref = None
                    # Fall through to retry check below
                else:
                    return formatted

            except asyncio.CancelledError:
                raise  # Never swallow cancellation

            except asyncio.TimeoutError as exc:
                error_msg = f"Tool error: '{tool_name}' timed out after {effective_timeout}s."
                exc_ref = exc
                logger.warning(
                    "TOOL_TIMEOUT: tool=%s timeout=%.1fs",
                    tool_name, effective_timeout,
                )

            except Exception as exc:
                error_msg = f"Tool error: {exc}"
                exc_ref = exc

            # Check if retryable
            if attempt < MAX_RETRIES and self._is_transient(error_msg, exc_ref):
                logger.info(
                    "TOOL_RETRY: tool=%s attempt=%d/%d backoff=%.1fs error=%s",
                    tool_name, attempt + 1, MAX_RETRIES + 1,
                    RETRY_BACKOFF_S, error_msg[:100],
                )
                await asyncio.sleep(RETRY_BACKOFF_S)
                continue

            # Not retryable or retries exhausted
            logger.warning(
                "TOOL_FAILED: tool=%s attempts=%d error=%s",
                tool_name, attempt + 1, error_msg[:200],
            )
            return error_msg

        # Should not reach here, but satisfy type checker
        return error_msg  # type: ignore[possibly-undefined]
