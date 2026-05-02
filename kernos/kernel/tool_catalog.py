"""Universal Tool Catalog — registry of all available tools with one-line descriptions.

The surfacer LLM reads this catalog to determine which tools are
relevant for a given turn. Intent-based, not keyword-based.
"""
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CatalogEntry:
    """A tool in the universal catalog."""
    name: str              # tool name (unique)
    description: str       # one-line description for surfacer
    source: str            # "kernel" | "mcp" | "workspace"
    registered_at: str = ""
    # Workspace tool metadata (populated for source="workspace")
    home_space: str = ""         # space where this tool's data lives
    implementation: str = ""     # Python file implementing execute()
    stateful: bool = True        # whether tool needs home space for execution
    # WORKSHOP-EXTERNAL-SERVICE-PRIMITIVE metadata (populated when the
    # tool's descriptor declares the extended fields). These power
    # service-bound dispatch + runtime enforcement at invocation time.
    descriptor_file: str = ""        # filename of the .tool.json descriptor
    service_id: str = ""             # bound external service, or "" for internal tools
    registration_hash: str = ""      # SHA-256 of (descriptor || impl) at registration
    force_registered: bool = False   # author bypassed authoring-pattern validation
    # When set, the descriptor + implementation live at this absolute
    # directory rather than under the per-(instance, space) workspace
    # path. Used by stock connectors that ship tools in source. The
    # dispatcher resolves desc_path = stock_dir/descriptor_file and
    # impl_path = stock_dir/implementation when stock_dir is set.
    stock_dir: str = ""


# Token budget for tool schemas per reasoning call
TOOL_TOKEN_BUDGET = int(os.environ.get("KERNOS_TOOL_TOKEN_BUDGET", "8000"))

# Pinned tools: always loaded, never evicted (~25% of budget)
# These are the tools the agent needs on almost every turn.
#
# COGNITIVE-CONTEXT-V1 C5 added ``request_tool`` to this set per
# spec: the meta-recovery activation tool must reach the model on
# every turn so the agent can request a missing capability when
# its current set lacks the right tool. Pre-C5 it was conditionally
# surfaced by the assemble's analyzer (often missed); now it's
# unconditionally pinned alongside the other always-loaded tools.
ALWAYS_PINNED: set[str] = {
    "remember",           # memory retrieval
    "remember_details",   # deep memory retrieval
    "write_file",         # file creation
    "read_file",          # file reading
    "list_files",         # file listing
    "execute_code",       # workspace engine
    "register_tool",      # tool registration
    "request_tool",       # meta-recovery activation (CCV1 C5)
    "inspect_state",      # self-awareness + space listing
    "manage_workspace",   # artifact tracking
    "send_to_channel",    # communication
    "manage_plan",        # self-directed execution
    "send_relational_message",     # agent-to-agent send (RELATIONAL-MESSAGING)
    "resolve_relational_message",  # agent-to-agent resolution
    "manage_members",              # member + relationship management (catalog-scan misses "declare full-access toward X")
}

# Common MCP tools that get priority in the active window (not pinned, but preferred)
COMMON_MCP_NAMES: set[str] = {
    "get-current-time",
    "create-event",
    "list-events",
    "brave_web_search",
}


SURFACER_SCHEMA = {
    "type": "object",
    "properties": {
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tool names relevant to this request",
        },
    },
    "required": ["tools"],
    "additionalProperties": False,
}


class ToolCatalog:
    """Registry of all available tools with one-line descriptions and version tracking."""

    def __init__(self) -> None:
        self._entries: dict[str, CatalogEntry] = {}
        self.version: int = 0

    def register(self, name: str, description: str, source: str, registered_at: str = "") -> None:
        """Register a tool. Increments version if this is a new tool."""
        is_new = name not in self._entries
        self._entries[name] = CatalogEntry(
            name=name, description=description,
            source=source, registered_at=registered_at,
        )
        if is_new:
            self.version += 1
            logger.info("TOOL_CATALOG: registered=%s source=%s version=%d", name, source, self.version)

    def unregister(self, name: str) -> None:
        """Remove a tool. Increments version if the tool existed."""
        if name in self._entries:
            del self._entries[name]
            self.version += 1
            logger.info("TOOL_CATALOG: unregistered=%s version=%d", name, self.version)

    def get(self, name: str) -> CatalogEntry | None:
        return self._entries.get(name)

    def get_all(self) -> list[CatalogEntry]:
        return list(self._entries.values())

    def get_names(self) -> set[str]:
        return set(self._entries.keys())

    def build_catalog_text(self, exclude: set[str] | None = None) -> str:
        """Build a compact text listing of all tools for the surfacer LLM."""
        lines = []
        _exclude = exclude or set()
        for entry in sorted(self._entries.values(), key=lambda e: e.name):
            if entry.name not in _exclude:
                lines.append(f"- {entry.name}: {entry.description}")
        return "\n".join(lines)

    def has_workspace_tool(self, name: str) -> bool:
        """Check if a tool is a registered workspace tool."""
        entry = self._entries.get(name)
        return entry is not None and entry.source == "workspace"

    def get_tools_since_version(self, since_version: int) -> list[CatalogEntry]:
        """Get tools added since a given version. Approximation: returns all if version gap exists."""
        # Since we don't track per-entry version, return all entries when version mismatch detected.
        # This is fine — the scan LLM filters for relevance.
        return self.get_all()

    def disabled_tool_names(self, disabled_service_ids: set[str]) -> set[str]:
        """Return tool names whose service_id is in the disabled set.

        Per INSTALL-FOR-STOCK-CONNECTORS Section 2 (surfacing layer):
        disabled service tools never surface, regardless of any other
        relevance signal. The catalog still holds the entries (so
        `kernos services list` can show them as available-but-disabled);
        callers building agent-facing tool lists pass these names into
        their `exclude` set so the surfacer never offers them.

        Tools without a service_id (kernel tools, MCP tools) are never
        in the result — they're install-level always-available.
        """
        if not disabled_service_ids:
            return set()
        return {
            entry.name
            for entry in self._entries.values()
            if entry.service_id and entry.service_id in disabled_service_ids
        }
