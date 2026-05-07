"""Canvas primitive — scoped, named directories of markdown pages.

CANVAS-V1 (SPEC-CANVAS-V1). A canvas is a shared-state primitive for
accumulating structured content across turns and members: world-building
notes, decision history, project logs, household planning, campaign
state. Three scope tiers (personal / specific / team). Pages are
markdown files with YAML frontmatter. Page types (note / decision / log)
are advisory; state vocabulary is type-specific.

File layout on disk:
    data/{instance_id}/canvases/{canvas_id}/
        canvas.yaml         - canvas-level metadata
        index.md            - landing page (auto-created)
        {slug}.md           - content pages
        {slug}.v{N}.md      - prior versions (retained on page_write)

Storage model:
    - Canvas registry rides the existing instance.db shared_spaces table
      (repurposed from its vestigial V2 slot; see CANVAS-V1 spec).
    - canvas_members table holds explicit membership for personal + specific
      scopes. Team-scope canvases don't need per-member rows; the access
      check in ``member_has_canvas_access`` short-circuits on scope='team'.

Discipline:
    - Canvas writes do NOT go through conversation_log / compaction.
      Canvases are shared artifacts, not turn-level conversation state.
    - Page types are advisory only; they don't enter the dispatch gate's
      effect classification.
    - Out-of-scope members must not see the canvas exists — enforcement
      at tool + disclosure-gate layer (see filter_canvases_by_membership).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kernos.utils import _safe_name, utc_now

logger = logging.getLogger(__name__)


VALID_SCOPES = ("personal", "specific", "team")

#: Advisory — page-type state vocabularies. v1 accepts writes with any
#: state string (types are advisory per spec); this table drives the
#: routes detector + default templates. Logs have no routable states
#: (append-only semantics).
PAGE_TYPE_STATES: dict[str, tuple[str, ...]] = {
    "note": ("drafted", "current", "archived"),
    "decision": ("proposed", "ratified", "superseded"),
    "log": (),
}

DEFAULT_PAGE_TYPE = "note"

#: Instance default for operator-in-loop preferences (see Pillar 5).
DEFAULT_CONSULT_OPERATOR_AT = ("shipped", "on_conflict")

#: Whole-phrase weight for page_search ranking. Used in the
#: ``matches`` field returned to callers (a single composite scalar
#: for ergonomic consumption); ranking itself sorts by the
#: ``(phrase_count, token_sum)`` tuple so an exact-phrase hit
#: STRICTLY outranks any number of scattered-token hits, regardless
#: of how high the token sum climbs.
_PHRASE_BONUS = 100


# ---------------------------------------------------------------------------
# Section markers (SECTION-MARKERS-AND-GARDENER Pillar 1)
# ---------------------------------------------------------------------------
#: Marker comment carries summary / tokens / last_updated as quoted/plain
#: key=value pairs. Invisible to Markdown renderers (Obsidian, GitHub,
#: VSCode preview) because it's a standard HTML comment. Sections are
#: H2 (``## ``) boundaries; anything above the first H2 is top-level
#: page content and lives outside any section.
_SECTION_HEADING_RE = re.compile(r"^(##\s+)(.+?)\s*$", re.MULTILINE)
_SECTION_MARKER_RE = re.compile(
    r"<!--\s*section:\s*(?P<fields>.*?)\s*-->", re.DOTALL,
)
_MARKER_FIELD_RE = re.compile(
    r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|(\S+))',
)


def _slugify_heading(text: str) -> str:
    """Lowercase, hyphen-joined, punctuation-stripped heading slug."""
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-")


def _estimate_tokens(text: str) -> int:
    """Cheap token approximation. Not exact — that's fine for marker display.

    Whitespace-tokenize; charge ~1.3 tokens per word to roughly match
    subword tokenizer behavior on English text. Caller reads this as a
    budget-planning hint, not a billing-grade count.
    """
    if not text:
        return 0
    words = len(text.split())
    return int(words * 1.3) + 1


@dataclass
class Section:
    """One page section — heading + optional marker + body (until next H2)."""
    slug: str
    heading: str             # the heading text (without the leading "## ")
    body: str                # everything between heading/marker and next H2
    marker_summary: str = ""
    marker_tokens: int = 0
    marker_last_updated: str = ""
    has_marker: bool = False


def _parse_marker(comment: str) -> dict:
    """Parse ``summary="x" tokens=430 last_updated=2026-04-19`` field pairs."""
    out: dict = {}
    for match in _MARKER_FIELD_RE.finditer(comment or ""):
        key = match.group(1)
        raw_quoted = match.group(2)
        raw_plain = match.group(3)
        if raw_quoted is not None:
            out[key] = raw_quoted.replace('\\"', '"').replace("\\\\", "\\")
        else:
            out[key] = raw_plain
    return out


def _render_marker(summary: str, tokens: int, last_updated: str) -> str:
    """Render a ``<!-- section: ... -->`` comment. Escapes quotes in summary."""
    safe_summary = (summary or "").replace("\\", "\\\\").replace('"', '\\"')
    parts = [f'summary="{safe_summary}"', f"tokens={tokens}"]
    if last_updated:
        parts.append(f"last_updated={last_updated}")
    return f"<!-- section: {' '.join(parts)} -->"


def parse_sections(body: str) -> tuple[str, list[Section]]:
    """Split a page body into (preamble, sections).

    ``preamble`` is the text above the first H2 heading. Each ``Section``
    covers heading through the char before the next H2 (or end of body).
    Returns ``(body, [])`` unchanged when no H2 headings exist — this is
    the backward-compat guarantee for pre-section-markers pages.
    """
    if not body:
        return "", []
    matches = list(_SECTION_HEADING_RE.finditer(body))
    if not matches:
        return body, []

    preamble = body[:matches[0].start()].rstrip("\n")

    sections: list[Section] = []
    for i, m in enumerate(matches):
        heading_text = m.group(2).strip()
        slug = _slugify_heading(heading_text)
        section_start = m.end()
        section_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section_body_raw = body[section_start:section_end]

        # Extract an optional marker comment from the first few lines.
        marker_match = _SECTION_MARKER_RE.search(section_body_raw[:1024])
        if marker_match and marker_match.start() < 200:
            fields = _parse_marker(marker_match.group("fields"))
            # Strip the marker (and surrounding blank line) from the body.
            body_text = (
                section_body_raw[:marker_match.start()]
                + section_body_raw[marker_match.end():]
            )
            # Normalize leading blank lines
            body_text = body_text.lstrip("\n")
            body_text = body_text.rstrip("\n")
            sections.append(Section(
                slug=slug, heading=heading_text,
                body=body_text,
                marker_summary=fields.get("summary", ""),
                marker_tokens=int(fields.get("tokens") or 0),
                marker_last_updated=fields.get("last_updated", ""),
                has_marker=True,
            ))
        else:
            sections.append(Section(
                slug=slug, heading=heading_text,
                body=section_body_raw.lstrip("\n").rstrip("\n"),
            ))
    return preamble, sections


def render_sections(preamble: str, sections: list[Section]) -> str:
    """Reassemble body from preamble + sections, preserving markers."""
    parts: list[str] = []
    if preamble:
        parts.append(preamble.rstrip("\n"))
    for s in sections:
        chunks = [f"## {s.heading}"]
        if s.has_marker:
            chunks.append(
                _render_marker(s.marker_summary, s.marker_tokens, s.marker_last_updated)
            )
        if s.body:
            chunks.append(s.body.rstrip("\n"))
        parts.append("\n".join(chunks))
    return "\n\n".join(p for p in parts if p) + ("\n" if parts else "")


def render_summary_view(preamble: str, sections: list[Section]) -> str:
    """TOC-style outline: preamble + heading + marker summary per section.

    Body content of each section is omitted. Intended for large pages
    where the agent wants a navigable outline without spending the
    context budget on full bodies.
    """
    parts: list[str] = []
    if preamble:
        parts.append(preamble.rstrip("\n"))
    for s in sections:
        line = f"## {s.heading}"
        if s.marker_summary:
            line += f"\n> {s.marker_summary}"
        elif s.has_marker:
            line += "\n> _(summary marker present but empty)_"
        else:
            tokens_approx = _estimate_tokens(s.body)
            line += f"\n> _(no summary; ~{tokens_approx} tokens in body)_"
        parts.append(line)
    return "\n\n".join(parts) + ("\n" if parts else "")


def maybe_refresh_section_tokens(body: str, last_updated: str = "") -> str:
    """Recompute every section's ``tokens`` marker and return the new body.

    ``last_updated`` (when non-empty) is stamped on every section whose
    body differs from what the existing marker implies. Sections without
    markers stay without markers — we don't add markers implicitly; the
    agent or Gardener opts in by declaring markers.
    """
    preamble, sections = parse_sections(body)
    if not sections:
        return body
    changed = False
    for s in sections:
        if not s.has_marker:
            continue
        new_tokens = _estimate_tokens(s.body)
        if new_tokens != s.marker_tokens:
            s.marker_tokens = new_tokens
            changed = True
        if last_updated and not s.marker_last_updated:
            s.marker_last_updated = last_updated
            changed = True
    if not changed:
        return body
    return render_sections(preamble, sections)


_INITIAL_SHAPE_RE = re.compile(
    r"^##\s+Initial canvas shape\s*\n(?P<body>.*?)(?=\n##\s+|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)
_SHAPE_BULLET_RE = re.compile(
    r"^-\s+"                         # bullet marker
    r"`(?P<path>[^`]+)`"             # `path` in backticks
    r"(?:\s*\((?P<meta>[^)]*)\))?"   # optional (meta) — type, scope, etc.
    r"(?:\s*[—-]+\s*(?P<desc>.+))?"  # optional — description
    r"\s*$",
    re.MULTILINE,
)
_VALID_PAGE_TYPES = {"note", "decision", "log"}
_VALID_SCOPES_FOR_PARSE = {"personal", "specific", "team"}


def parse_initial_shape(pattern_body: str) -> list[dict]:
    """Extract the pattern's "Initial canvas shape" section into page specs.

    Each returned dict has ``path``, ``type`` (defaults to ``note``),
    ``scope`` (defaults to ``team``), and ``description``. Sub-bullets
    (those starting with additional indentation) are attached to their
    parent as ``sub_bullets`` but not recursively parsed — v1 creates
    parent pages only; sub-templates can be added by the Gardener or
    the member post-creation.

    Returns an empty list when the pattern has no Initial canvas shape
    section — callers fall back to a minimal canvas.
    """
    if not pattern_body:
        return []
    section_match = _INITIAL_SHAPE_RE.search(pattern_body)
    if not section_match:
        return []
    section_body = section_match.group("body")

    out: list[dict] = []
    # Parse only top-level bullets (those starting with "- " at line-start).
    # Trailing bullets (sub-bullets) are indented and skipped.
    for m in _SHAPE_BULLET_RE.finditer(section_body):
        path = (m.group("path") or "").strip()
        if not path:
            continue
        # Skip "subdirectory" placeholders — e.g. `specs/` without an .md suffix
        # and no explicit file indicator. Pattern-declared subdirs are hints,
        # not concrete initial pages.
        if path.endswith("/"):
            continue
        meta_raw = (m.group("meta") or "").strip()
        desc = (m.group("desc") or "").strip()
        page_type = "note"
        scope = "team"
        flags: list[str] = []
        if meta_raw:
            for token in (t.strip().lower() for t in meta_raw.split(",")):
                if token in _VALID_PAGE_TYPES:
                    page_type = token
                elif token in _VALID_SCOPES_FOR_PARSE:
                    scope = token
                elif token:
                    flags.append(token)
        entry = {
            "path": path if path.endswith(".md") else f"{path}.md",
            "type": page_type,
            "scope": scope,
            "description": desc,
        }
        if flags:
            entry["flags"] = flags
        out.append(entry)
    return out


def replace_section_body(body: str, slug: str, new_body: str,
                          last_updated: str = "") -> tuple[str, bool]:
    """Surgically replace one section's body in an existing page body.

    Returns ``(new_body, replaced)``. Non-existent slug returns the
    original body with ``replaced=False``. Used by ``page_write`` when
    the caller passes a ``section=`` argument.
    """
    preamble, sections = parse_sections(body)
    if not sections:
        return body, False
    for s in sections:
        if s.slug == slug:
            s.body = (new_body or "").rstrip("\n")
            if s.has_marker:
                s.marker_tokens = _estimate_tokens(s.body)
                if last_updated:
                    s.marker_last_updated = last_updated
            return render_sections(preamble, sections), True
    return body, False


# ---------------------------------------------------------------------------
# Routes-lite (Pillar 5)
# ---------------------------------------------------------------------------
#: Target prefixes the routes engine understands. Space targets
#: ("space:foo") are parsed but return a structured
#: ``route_target_not_supported_in_v1`` marker — deferred to v2.
ROUTE_TARGET_OPERATOR = "operator"
ROUTE_TARGET_MEMBER_PREFIX = "member:"
ROUTE_TARGET_SPACE_PREFIX = "space:"


def resolve_consult_operator_at(
    page_value: Any, canvas_default: Any, instance_default: tuple[str, ...] = DEFAULT_CONSULT_OPERATOR_AT,
) -> list[str]:
    """Shallow inheritance for consult_operator_at.

    Resolution order (first non-None wins, replacing — never merged):
        1. ``page_value``      — explicit page-level ``consult_operator_at``
        2. ``canvas_default``  — canvas-level default from canvas.yaml
        3. ``instance_default``— module constant

    None means "unset"; an empty list ``[]`` means "never consult" and is a
    valid explicit override. That's why we can't fall back on falsy.
    """
    for candidate in (page_value, canvas_default):
        if candidate is None:
            continue
        if isinstance(candidate, (list, tuple)):
            return [str(x) for x in candidate]
        if isinstance(candidate, str):
            return [candidate]
    return list(instance_default)


def parse_route_targets(routes: Any, state: str) -> list[str]:
    """Pull the target list for a given state out of a routes dict.

    Accepts string or list forms: ``{'ratified': 'operator'}`` or
    ``{'ratified': ['operator', 'member:abc']}``. Unknown or missing state
    returns an empty list.
    """
    if not isinstance(routes, dict) or not state:
        return []
    val = routes.get(state)
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, (list, tuple)):
        return [str(x) for x in val]
    return []


def classify_route_target(target: str) -> tuple[str, str]:
    """Return ``(kind, arg)`` where kind ∈ {operator, member, space, unknown}."""
    t = (target or "").strip()
    if t == ROUTE_TARGET_OPERATOR:
        return ("operator", "")
    if t.startswith(ROUTE_TARGET_MEMBER_PREFIX):
        return ("member", t[len(ROUTE_TARGET_MEMBER_PREFIX):])
    if t.startswith(ROUTE_TARGET_SPACE_PREFIX):
        return ("space", t[len(ROUTE_TARGET_SPACE_PREFIX):])
    return ("unknown", t)


# ---------------------------------------------------------------------------
# Frontmatter parse + serialize
# ---------------------------------------------------------------------------


_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?\n)---\s*\n?(.*)$", re.DOTALL,
)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a markdown string.

    Returns (frontmatter_dict, body_text). If the text has no frontmatter
    block, returns ({}, text) unchanged.
    """
    m = _FRONTMATTER_PATTERN.match(text or "")
    if not m:
        return {}, text or ""
    fm_text, body = m.group(1), m.group(2)
    try:
        parsed = yaml.safe_load(fm_text) or {}
        if not isinstance(parsed, dict):
            return {}, text
        return parsed, body
    except yaml.YAMLError as exc:
        logger.warning("CANVAS_FRONTMATTER_PARSE_FAILED: %s", exc)
        return {}, text


def serialize_frontmatter(frontmatter: dict, body: str) -> str:
    """Render frontmatter dict + body as a markdown string with ``---`` fences."""
    if not frontmatter:
        return body or ""
    fm_text = yaml.safe_dump(
        frontmatter, sort_keys=False, default_flow_style=False,
    ).rstrip()
    return f"---\n{fm_text}\n---\n{body or ''}"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Canvas:
    """Canvas registry record + on-disk layout pointer."""

    canvas_id: str
    name: str
    scope: str
    owner_member_id: str
    created_at: str
    description: str = ""
    pinned_to_spaces: list[str] = field(default_factory=list)
    default_page_type: str = DEFAULT_PAGE_TYPE
    default_consult_operator_at: list[str] = field(
        default_factory=lambda: list(DEFAULT_CONSULT_OPERATOR_AT),
    )
    canvas_yaml_path: str = ""
    status: str = "active"

    def to_yaml_dict(self) -> dict:
        """Shape serialized into canvas.yaml on disk."""
        return {
            "name": self.name,
            "scope": self.scope,
            "owner": self.owner_member_id,
            "created_at": self.created_at,
            "default_page_type": self.default_page_type,
            "default_consult_operator_at": list(self.default_consult_operator_at),
            "pinned_to_spaces": list(self.pinned_to_spaces),
        }


@dataclass
class CanvasPage:
    """A single page inside a canvas. Frontmatter + markdown body."""

    canvas_id: str
    path: str                   # relative path inside the canvas dir
    title: str = ""
    type: str = DEFAULT_PAGE_TYPE
    state: str = ""
    body: str = ""
    last_updated: str = ""
    last_updated_by: str = ""
    watchers: list[str] = field(default_factory=list)
    routes: dict[str, Any] = field(default_factory=dict)
    consult_operator_at: list[str] | None = None
    extra: dict = field(default_factory=dict)

    def to_frontmatter(self) -> dict:
        fm: dict = {
            "title": self.title,
            "type": self.type,
            "last_updated": self.last_updated,
            "last_updated_by": self.last_updated_by,
        }
        if self.state:
            fm["state"] = self.state
        if self.watchers:
            fm["watchers"] = list(self.watchers)
        if self.routes:
            fm["routes"] = dict(self.routes)
        if self.consult_operator_at is not None:
            fm["consult_operator_at"] = list(self.consult_operator_at)
        # Preserve any extra fields the agent included
        for k, v in self.extra.items():
            if k not in fm:
                fm[k] = v
        return fm

    def to_markdown(self) -> str:
        return serialize_frontmatter(self.to_frontmatter(), self.body)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_canvas_id() -> str:
    return f"canvas_{uuid.uuid4().hex[:12]}"


def _validate_page_slug(slug: str) -> bool:
    """Canvas page paths are relative; allow slashes for nesting but not
    path escape or absolute paths."""
    if not slug or ".." in slug:
        return False
    if slug.startswith("/") or "\\" in slug:
        return False
    # Allow one trailing .md or no extension; canvas.py adds .md
    return True


def canvas_dir(data_dir: str, instance_id: str, canvas_id: str) -> Path:
    return (
        Path(data_dir)
        / _safe_name(instance_id)
        / "canvases"
        / canvas_id
    )


def _page_path_in_canvas(canvas_root: Path, page_slug: str) -> Path:
    """Resolve a page slug under canvas_root, enforcing no-escape.

    Accepts slug with or without ``.md`` suffix; appends ``.md`` if absent.
    Rejects absolute paths and ``..`` segments.
    """
    if not _validate_page_slug(page_slug):
        raise ValueError(f"Invalid page slug: {page_slug!r}")
    name = page_slug if page_slug.endswith(".md") else f"{page_slug}.md"
    candidate = (canvas_root / name).resolve()
    root = canvas_root.resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError(
            f"Page path escapes canvas directory: {page_slug!r}"
        )
    return candidate


# ---------------------------------------------------------------------------
# CanvasService
# ---------------------------------------------------------------------------


@dataclass
class CanvasOpResult:
    ok: bool
    canvas_id: str = ""
    page_path: str = ""
    error: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"ok": self.ok}
        if self.canvas_id:
            out["canvas_id"] = self.canvas_id
        if self.page_path:
            out["page_path"] = self.page_path
        if self.error:
            out["error"] = self.error
        for k, v in self.extra.items():
            out[k] = v
        return out


class CanvasService:
    """Orchestrates canvas create/read/write/list + frontmatter roundtrip.

    Instance_db holds the registry + membership; this service handles the
    on-disk files and the Canvas dataclass lifecycle. Notifications,
    watchers, and routes are layered on top (Pillars 4+5).
    """

    def __init__(
        self,
        instance_db: Any,
        data_dir: str = "./data",
        event_emit: Any = None,
    ) -> None:
        self._db = instance_db
        self._data_dir = data_dir
        #: Optional ``async (instance_id, event_type, payload, *, member_id)``
        #: callable for event-stream emission. Best-effort: every emit is
        #: wrapped in try/except — canvas ops never break on event failure.
        self._event_emit = event_emit
        # Page-frontmatter cache keyed on absolute path; invalidated on write
        self._fm_cache: dict[str, tuple[float, dict, str]] = {}
        #: Per-canvas reference index (CANVAS-CROSS-PAGE-INDEX). Loaded
        #: lazily on first read/write; rebuilt from pages on cold start
        #: (missing or malformed on-disk file).
        self._ref_indexes: dict[str, Any] = {}

    async def _emit(self, instance_id: str, event_type: str, payload: dict,
                    member_id: str = "") -> None:
        if not self._event_emit:
            return
        try:
            await self._event_emit(
                instance_id, event_type, payload, member_id=member_id,
            )
        except Exception as exc:  # noqa: BLE001 — never break canvas on event
            logger.debug("CANVAS_EVENT_EMIT_FAILED: %s %s", event_type, exc)

    # ---- Create ----------------------------------------------------------

    async def create(
        self,
        *,
        instance_id: str,
        creator_member_id: str,
        name: str,
        scope: str,
        members: list[str] | None = None,
        description: str = "",
        default_page_type: str = DEFAULT_PAGE_TYPE,
        pinned_to_spaces: list[str] | None = None,
    ) -> CanvasOpResult:
        if scope not in VALID_SCOPES:
            return CanvasOpResult(
                ok=False,
                error=f"Unknown scope {scope!r}; expected one of {list(VALID_SCOPES)}.",
            )
        if not name or not name.strip():
            return CanvasOpResult(ok=False, error="Canvas name is required.")
        if scope == "specific" and not members:
            return CanvasOpResult(
                ok=False,
                error="scope='specific' requires an explicit members list.",
            )

        canvas_id = _generate_canvas_id()
        root = canvas_dir(self._data_dir, instance_id, canvas_id)
        root.mkdir(parents=True, exist_ok=True)

        canvas = Canvas(
            canvas_id=canvas_id,
            name=name.strip(),
            scope=scope,
            owner_member_id=creator_member_id,
            created_at=utc_now(),
            description=description,
            pinned_to_spaces=list(pinned_to_spaces or []),
            default_page_type=default_page_type,
            canvas_yaml_path=str(root / "canvas.yaml"),
        )

        # Write canvas.yaml
        yaml_text = yaml.safe_dump(
            canvas.to_yaml_dict(), sort_keys=False, default_flow_style=False,
        )
        (root / "canvas.yaml").write_text(yaml_text, encoding="utf-8")

        # Seed index.md
        index_page = CanvasPage(
            canvas_id=canvas_id,
            path="index.md",
            title=canvas.name,
            type=default_page_type,
            state=PAGE_TYPE_STATES.get(default_page_type, ("",))[0] or "",
            body=f"# {canvas.name}\n\n{description or 'Canvas landing page.'}\n",
            last_updated=canvas.created_at,
            last_updated_by=creator_member_id,
        )
        (root / "index.md").write_text(index_page.to_markdown(), encoding="utf-8")

        # Persist registry row
        await self._db.save_canvas({
            "canvas_id": canvas_id,
            "name": canvas.name,
            "description": description,
            "status": "active",
            "created_at": canvas.created_at,
            "scope": scope,
            "owner_member_id": creator_member_id,
            "pinned_to_spaces": json.dumps(list(pinned_to_spaces or [])),
            "canvas_yaml_path": str(root / "canvas.yaml"),
        })

        # Membership. Personal + specific: explicit rows. Team: owner only
        # (team scope accesses via scope='team' shortcut in the DB check).
        if scope == "personal":
            await self._db.add_canvas_member(
                canvas_id=canvas_id, member_id=creator_member_id,
            )
        elif scope == "specific":
            await self._db.add_canvas_member(
                canvas_id=canvas_id, member_id=creator_member_id,
            )
            for m in members or []:
                if m and m != creator_member_id:
                    await self._db.add_canvas_member(
                        canvas_id=canvas_id, member_id=m,
                    )
        elif scope == "team":
            # Owner still needs a row so list_canvas_members can enumerate
            await self._db.add_canvas_member(
                canvas_id=canvas_id, member_id=creator_member_id,
            )

        logger.info(
            "CANVAS_CREATED: instance=%s canvas=%s scope=%s owner=%s",
            instance_id, canvas_id, scope, creator_member_id,
        )

        # Notify members = everyone with access except the creator.
        # For team scope we cannot enumerate "all future members"; callers
        # should resolve to current instance members at notify time.
        if scope == "specific":
            notify = [m for m in (members or []) if m and m != creator_member_id]
        elif scope == "team":
            notify = ["__team__"]  # sentinel: dispatcher resolves to instance members
        else:
            notify = []

        await self._emit(
            instance_id, "canvas.created",
            {
                "canvas_id": canvas_id,
                "name": canvas.name,
                "scope": scope,
                "owner_member_id": creator_member_id,
                "notify": notify,
            },
            member_id=creator_member_id,
        )

        return CanvasOpResult(
            ok=True, canvas_id=canvas_id,
            extra={"name": canvas.name, "scope": scope, "notify": notify},
        )

    # ---- Preferences (CANVAS-GARDENER-PREFERENCE-CAPTURE) ----------------

    async def get_preferences(
        self, *, instance_id: str, canvas_id: str,
    ) -> dict:
        """Return confirmed preferences from canvas.yaml's ``preferences:`` map.

        Empty dict when no preferences are set. Callers reading for
        heuristic-dispatch consumption should treat missing keys as
        "no override" rather than errors.
        """
        defaults = await self._canvas_defaults(instance_id, canvas_id)
        prefs = defaults.get("preferences")
        return dict(prefs) if isinstance(prefs, dict) else {}

    async def set_preference(
        self, *, instance_id: str, canvas_id: str,
        name: str, value: Any,
    ) -> bool:
        """Persist a confirmed preference to canvas.yaml. Overwrites on match.

        ``name`` is the preference key (e.g. ``rsvp-routing``). ``value`` is
        any YAML-serializable scalar / list / dict — the string form a
        heuristic's ``threshold_preference`` or ``suppressed_by_preference``
        field will read.
        """
        return await self._mutate_canvas_yaml(
            instance_id, canvas_id,
            lambda data: data.setdefault("preferences", {}).__setitem__(name, value) or True,
        )

    async def remove_preference(
        self, *, instance_id: str, canvas_id: str, name: str,
    ) -> bool:
        """Delete a confirmed preference. Idempotent — missing key is a no-op True."""
        def _remove(data: dict) -> bool:
            prefs = data.get("preferences") or {}
            if isinstance(prefs, dict) and name in prefs:
                del prefs[name]
            return True
        return await self._mutate_canvas_yaml(instance_id, canvas_id, _remove)

    async def get_pending_preferences(
        self, *, instance_id: str, canvas_id: str,
    ) -> list[dict]:
        """Return pending preferences (awaiting confirmation) from canvas.yaml.

        Each entry: ``{name, value, effect_kind, evidence, surfaced_at,
        confidence, supersedes?}``. Order matches insertion.
        """
        defaults = await self._canvas_defaults(instance_id, canvas_id)
        pending = defaults.get("pending_preferences") or []
        if not isinstance(pending, list):
            return []
        return [dict(p) for p in pending if isinstance(p, dict)]

    async def add_pending_preference(
        self, *, instance_id: str, canvas_id: str, preference: dict,
    ) -> bool:
        """Append a preference to the pending list. Stamps surfaced_at if absent.

        Idempotent on preference name: if a pending preference with the
        same name already exists, this replaces it rather than appending a
        duplicate. Callers that want to superseded a prior confirmed
        preference should set ``preference["supersedes"]`` to that name.
        """
        name = (preference.get("name") or "").strip()
        if not name:
            return False
        entry = dict(preference)
        entry["name"] = name
        entry.setdefault("surfaced_at", utc_now())

        def _add(data: dict) -> bool:
            pending = data.get("pending_preferences")
            if not isinstance(pending, list):
                pending = []
                data["pending_preferences"] = pending
            # Replace-if-exists semantics.
            filtered = [p for p in pending if not (isinstance(p, dict) and p.get("name") == name)]
            filtered.append(entry)
            data["pending_preferences"] = filtered
            return True
        return await self._mutate_canvas_yaml(instance_id, canvas_id, _add)

    async def resolve_pending_preference(
        self, *, instance_id: str, canvas_id: str,
        name: str, action: str,
    ) -> dict | None:
        """Move a pending preference to confirmed (``action='confirm'``) or
        declined (``action='discard'``).

        Returns the resolved preference dict on success, or None when the
        pending entry wasn't found or the action is invalid.

        ``action='confirm'``: writes ``preferences[name] = value`` and
        removes from pending.

        ``action='discard'``: removes from pending and appends to
        ``declined_preferences`` (timestamped) so extraction can avoid
        re-proposing the same utterance shape within a decline window.
        """
        if action not in ("confirm", "discard"):
            return None

        resolved: dict | None = None

        def _resolve(data: dict) -> bool:
            nonlocal resolved
            pending = data.get("pending_preferences") or []
            if not isinstance(pending, list):
                return False
            remaining = []
            found: dict | None = None
            for entry in pending:
                if isinstance(entry, dict) and entry.get("name") == name:
                    found = entry
                else:
                    remaining.append(entry)
            if found is None:
                return False
            data["pending_preferences"] = remaining
            if action == "confirm":
                prefs = data.setdefault("preferences", {})
                if not isinstance(prefs, dict):
                    prefs = {}
                    data["preferences"] = prefs
                prefs[name] = found.get("value")
                resolved = {"action": "confirm", "name": name,
                            "value": found.get("value")}
            else:
                declined = data.setdefault("declined_preferences", [])
                if not isinstance(declined, list):
                    declined = []
                    data["declined_preferences"] = declined
                declined.append({
                    "name": name,
                    "value": found.get("value"),
                    "evidence": found.get("evidence", ""),
                    "declined_at": utc_now(),
                })
                resolved = {"action": "discard", "name": name,
                            "value": found.get("value")}
            return True

        ok = await self._mutate_canvas_yaml(instance_id, canvas_id, _resolve)
        return resolved if ok else None

    async def drop_expired_pending_preferences(
        self, *, instance_id: str, canvas_id: str, ttl_hours: int = 24,
    ) -> int:
        """Remove pending preferences whose ``surfaced_at`` is older than ttl.

        Returns the number of entries dropped. Called by the Gardener on
        event dispatch so pending preferences auto-expire even when the
        member never responds.
        """
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        now = _dt.now(_tz.utc)
        window = _td(hours=ttl_hours)
        dropped_count = 0

        def _drop(data: dict) -> bool:
            nonlocal dropped_count
            pending = data.get("pending_preferences") or []
            if not isinstance(pending, list):
                return False
            kept: list[dict] = []
            for entry in pending:
                if not isinstance(entry, dict):
                    continue
                stamped = entry.get("surfaced_at")
                if not stamped:
                    kept.append(entry)
                    continue
                try:
                    when = _dt.fromisoformat(str(stamped).replace("Z", "+00:00"))
                except ValueError:
                    kept.append(entry)
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_tz.utc)
                if (now - when) < window:
                    kept.append(entry)
                else:
                    dropped_count += 1
            if dropped_count:
                data["pending_preferences"] = kept
                return True
            return False

        await self._mutate_canvas_yaml(instance_id, canvas_id, _drop)
        return dropped_count

    async def _mutate_canvas_yaml(
        self, instance_id: str, canvas_id: str,
        mutator,
    ) -> bool:
        """Load canvas.yaml, run mutator(data) → bool, save if True.

        Shared mutation helper for preference read/write. Fails soft —
        missing file or parse error returns False rather than raising so
        canvas ops stay non-breaking.
        """
        try:
            root = canvas_dir(self._data_dir, instance_id, canvas_id)
            yaml_path = root / "canvas.yaml"
            if not yaml_path.is_file():
                return False
            parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            if not isinstance(parsed, dict):
                return False
            if mutator(parsed):
                yaml_path.write_text(
                    yaml.safe_dump(parsed, sort_keys=False, default_flow_style=False),
                    encoding="utf-8",
                )
                return True
            return True  # mutator declined to change anything — still OK
        except Exception as exc:  # noqa: BLE001
            logger.warning("CANVAS_YAML_MUTATE_FAILED: %s", exc)
            return False

    # ---- Reference index (CANVAS-CROSS-PAGE-INDEX) -----------------------

    async def get_reference_index(
        self, *, instance_id: str, canvas_id: str,
    ) -> Any:
        """Return the canvas's ReferenceIndex — cold-start rebuild on miss.

        Lazy: first call for a canvas loads ``references.json`` if present
        or rebuilds from canvas pages. Subsequent calls return the
        cached instance. Gardener heuristics consuming reference counts
        get their queries answered in memory.
        """
        from kernos.kernel.canvas_reference_index import (
            load_or_empty, rebuild_from_canvas, save,
        )
        cache_key = f"{instance_id}::{canvas_id}"
        cached = self._ref_indexes.get(cache_key)
        if cached is not None:
            return cached
        index = load_or_empty(self._data_dir, instance_id, canvas_id)
        # Cold-start rebuild: if the on-disk file is missing OR empty but
        # the canvas has pages with potential links, walk and populate.
        if not index.outbound:
            try:
                root = canvas_dir(self._data_dir, instance_id, canvas_id)
                if root.is_dir():
                    # Only pay rebuild cost when on-disk file doesn't exist.
                    from kernos.kernel.canvas_reference_index import index_path
                    if not index_path(self._data_dir, instance_id, canvas_id).is_file():
                        index = await rebuild_from_canvas(
                            canvas_service=self, instance_id=instance_id,
                            canvas_id=canvas_id,
                        )
                        save(index, self._data_dir, instance_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("CANVAS_REF_INDEX_LAZY_REBUILD_FAILED: %s", exc)
        self._ref_indexes[cache_key] = index
        return index

    async def _update_reference_index_for_write(
        self, *, instance_id: str, canvas_id: str, page_slug: str,
        new_body: str,
    ) -> list[Any]:
        """Update the reference index for the just-written page.

        Returns the list of BrokenReference findings for this write so
        the Gardener dispatch can surface them. Never raises — index
        failures log and degrade to empty findings.
        """
        from kernos.kernel.canvas_reference_index import (
            parse_wiki_links, save,
        )
        try:
            index = await self.get_reference_index(
                instance_id=instance_id, canvas_id=canvas_id,
            )
            targets = parse_wiki_links(new_body or "")
            index.update_page(page_slug, targets)
            save(index, self._data_dir, instance_id)
            # Broken-reference detection — only check THIS source's targets
            # (not the whole canvas) to keep the hot path bounded.
            known_pages = await self.page_list(
                instance_id=instance_id, canvas_id=canvas_id,
            )
            known_slugs = {p.get("path", "") for p in known_pages if p.get("path")}
            all_broken = index.find_broken_references(known_slugs)
            # Filter to this source only so we don't re-surface every
            # canvas-wide broken ref on every single write.
            from kernos.kernel.canvas_reference_index import slug_from_path
            this_source = slug_from_path(page_slug)
            return [b for b in all_broken if b.source_slug == this_source]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CANVAS_REF_INDEX_UPDATE_FAILED: canvas=%s page=%s reason=%s",
                canvas_id, page_slug, exc,
            )
            return []

    # ---- Canvas-level mutation (post-create) -----------------------------

    async def set_canvas_pattern(
        self, *, instance_id: str, canvas_id: str, pattern: str,
    ) -> bool:
        """Record ``pattern: <name>`` in the canvas's canvas.yaml on disk.

        Used by the Gardener after picking an initial shape (Pillar 3).
        Returns True on success, False on any IO or parse failure. Fails
        soft — an unrecorded pattern means the canvas keeps working; only
        the Gardener's future consultations lose the pattern hint.
        """
        try:
            root = canvas_dir(self._data_dir, instance_id, canvas_id)
            yaml_path = root / "canvas.yaml"
            if not yaml_path.is_file():
                return False
            parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            if not isinstance(parsed, dict):
                return False
            parsed["pattern"] = pattern
            yaml_path.write_text(
                yaml.safe_dump(parsed, sort_keys=False, default_flow_style=False),
                encoding="utf-8",
            )
            logger.info(
                "CANVAS_PATTERN_SET: canvas=%s pattern=%s",
                canvas_id, pattern,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("CANVAS_PATTERN_SET_FAILED: %s", exc)
            return False

    # ---- Canvas-level defaults (for route resolution) -------------------

    async def _canvas_defaults(
        self, instance_id: str, canvas_id: str,
    ) -> dict:
        """Read canvas.yaml for defaults — returns empty dict on any error."""
        root = canvas_dir(self._data_dir, instance_id, canvas_id)
        yaml_path = root / "canvas.yaml"
        if not yaml_path.is_file():
            return {}
        try:
            parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            if isinstance(parsed, dict):
                return parsed
        except yaml.YAMLError as exc:
            logger.warning("CANVAS_YAML_PARSE_FAILED: %s", exc)
        return {}

    # ---- Narrow helpers for internal seeders -----------------------------

    async def create_personal_canvas(
        self, *,
        instance_id: str,
        member_id: str,
        name: str,
        description: str = "",
        default_page_type: str = DEFAULT_PAGE_TYPE,
    ) -> CanvasOpResult:
        """Auto-create a personal canvas for a member.

        Narrower than :meth:`create` — no scope/members arguments, no
        notification fanout (personal scope notifies nobody). Intended for
        internal seeders (e.g. My Tools on onboarding) rather than the
        agent-facing ``canvas_create`` tool path.
        """
        return await self.create(
            instance_id=instance_id, creator_member_id=member_id,
            name=name, scope="personal",
            description=description, default_page_type=default_page_type,
        )

    # ---- Page read -------------------------------------------------------

    async def page_read(
        self, *, instance_id: str, canvas_id: str, page_slug: str,
        mode: str = "full", section: str = "",
    ) -> CanvasOpResult:
        """Read a page. Modes (SECTION-MARKERS Pillar 1):

        - ``full`` (default): whole body + frontmatter. V1 behavior.
        - ``summary``: frontmatter + preamble + per-section headings and
          marker summaries (section bodies omitted). Large pages become
          navigable outlines.
        - ``section``: requires ``section`` (slug); returns just that
          section's body + its marker metadata. Unknown slug = error.

        Pages without H2 headings have no sections — ``summary`` falls
        back to the full body, ``section`` errors with an informative
        message. Backward-compat for all pages predating this feature.
        """
        root = canvas_dir(self._data_dir, instance_id, canvas_id)
        try:
            page_path = _page_path_in_canvas(root, page_slug)
        except ValueError as exc:
            return CanvasOpResult(ok=False, error=str(exc))
        if not page_path.is_file():
            return CanvasOpResult(
                ok=False, page_path=page_slug,
                error=f"Page not found: {page_slug!r}",
            )
        frontmatter, body = self._read_page_cached(page_path)

        mode = (mode or "full").lower()
        if mode == "full":
            return CanvasOpResult(
                ok=True, canvas_id=canvas_id, page_path=page_slug,
                extra={"frontmatter": frontmatter, "body": body, "mode": "full"},
            )
        if mode == "summary":
            preamble, sections = parse_sections(body)
            view = render_summary_view(preamble, sections)
            return CanvasOpResult(
                ok=True, canvas_id=canvas_id, page_path=page_slug,
                extra={
                    "frontmatter": frontmatter,
                    "body": view,
                    "mode": "summary",
                    "section_count": len(sections),
                    "section_slugs": [s.slug for s in sections],
                },
            )
        if mode == "section":
            if not section:
                return CanvasOpResult(
                    ok=False, page_path=page_slug,
                    error="section mode requires a section slug",
                )
            _, sections = parse_sections(body)
            for s in sections:
                if s.slug == section:
                    return CanvasOpResult(
                        ok=True, canvas_id=canvas_id, page_path=page_slug,
                        extra={
                            "frontmatter": frontmatter,
                            "body": s.body,
                            "mode": "section",
                            "section": s.slug,
                            "heading": s.heading,
                            "marker_summary": s.marker_summary,
                            "marker_tokens": s.marker_tokens,
                            "marker_last_updated": s.marker_last_updated,
                            "has_marker": s.has_marker,
                        },
                    )
            return CanvasOpResult(
                ok=False, page_path=page_slug,
                error=f"Section not found: {section!r}",
            )
        return CanvasOpResult(
            ok=False, page_path=page_slug,
            error=f"Unknown read mode: {mode!r}",
        )

    def _read_page_cached(self, page_path: Path) -> tuple[dict, str]:
        """Mtime-invalidated parse of a page's frontmatter + body."""
        try:
            mtime = page_path.stat().st_mtime
        except OSError:
            return {}, ""
        key = str(page_path)
        cached = self._fm_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1], cached[2]
        text = page_path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(text)
        self._fm_cache[key] = (mtime, frontmatter, body)
        return frontmatter, body

    # ---- Page write ------------------------------------------------------

    async def page_write(
        self,
        *,
        instance_id: str,
        canvas_id: str,
        page_slug: str,
        body: str,
        writer_member_id: str,
        title: str | None = None,
        page_type: str | None = None,
        state: str | None = None,
        frontmatter_overrides: dict | None = None,
        section: str = "",
    ) -> CanvasOpResult:
        """Write a page (or one section of a page).

        When ``section`` is empty, ``body`` replaces the full page body
        (v1 behavior). When ``section=<slug>`` is passed, ``body`` replaces
        just that section's body — the rest of the page is preserved,
        and the section's marker metadata is updated (tokens +
        last_updated). Section-targeted writes on pages that have no
        matching slug return a structured error without modifying the
        file.
        """
        root = canvas_dir(self._data_dir, instance_id, canvas_id)
        root.mkdir(parents=True, exist_ok=True)
        try:
            page_path = _page_path_in_canvas(root, page_slug)
        except ValueError as exc:
            return CanvasOpResult(ok=False, error=str(exc))

        # Preserve existing frontmatter when updating
        existing_fm: dict = {}
        prev_state: str = ""
        existing_body: str = ""
        if page_path.is_file():
            existing_fm, existing_body = self._read_page_cached(page_path)
            prev_state = (existing_fm.get("state") or "").strip()
            # Version retention: copy old to .v{N}.md before overwrite
            version_num = 1
            while (
                page_path.parent / f"{page_path.stem}.v{version_num}.md"
            ).exists():
                version_num += 1
            try:
                old_text = page_path.read_text(encoding="utf-8")
                (
                    page_path.parent / f"{page_path.stem}.v{version_num}.md"
                ).write_text(old_text, encoding="utf-8")
            except OSError as exc:
                logger.warning("CANVAS_VERSION_RETAIN_FAILED: %s", exc)
        else:
            page_path.parent.mkdir(parents=True, exist_ok=True)

        # Merge frontmatter: existing < explicit args < overrides
        new_fm = dict(existing_fm)
        new_fm["title"] = title or existing_fm.get("title") or page_slug
        new_fm["type"] = (
            page_type
            or existing_fm.get("type")
            or DEFAULT_PAGE_TYPE
        )
        if state is not None:
            new_fm["state"] = state
        if frontmatter_overrides:
            new_fm.update(frontmatter_overrides)
        new_fm["last_updated"] = utc_now()
        new_fm["last_updated_by"] = writer_member_id

        # Resolve the new body. Section-targeted writes replace one
        # section in the existing body; full writes use ``body`` as-is.
        if section:
            if not existing_body:
                return CanvasOpResult(
                    ok=False, page_path=page_slug,
                    error=(
                        f"Section-targeted write to {page_slug!r} but page has "
                        "no existing body (or no sections parsed)."
                    ),
                )
            new_body, replaced = replace_section_body(
                existing_body, section, body or "",
                last_updated=new_fm["last_updated"],
            )
            if not replaced:
                return CanvasOpResult(
                    ok=False, page_path=page_slug,
                    error=f"Section not found: {section!r}",
                )
        else:
            new_body = body or ""

        # Refresh section tokens markers for all marker-bearing sections.
        # Cheap pass; no-op on pages without markers.
        new_body = maybe_refresh_section_tokens(
            new_body, last_updated=new_fm["last_updated"],
        )

        rendered = serialize_frontmatter(new_fm, new_body)
        page_path.write_text(rendered, encoding="utf-8")

        # Invalidate cache entry
        self._fm_cache.pop(str(page_path), None)

        # Flag advisory type issues
        declared_type = new_fm.get("type", DEFAULT_PAGE_TYPE)
        type_ok = declared_type in PAGE_TYPE_STATES
        new_state = (new_fm.get("state") or "").strip()

        logger.info(
            "CANVAS_PAGE_WRITTEN: canvas=%s page=%s by=%s type=%s state=%s prev_state=%s",
            canvas_id, page_slug, writer_member_id, declared_type,
            new_state or "(none)", prev_state or "(none)",
        )

        is_new = not existing_fm
        state_changed = bool(new_state and new_state != prev_state)

        # Routes + consult_operator_at resolution (Pillar 5).
        canvas_defaults = await self._canvas_defaults(instance_id, canvas_id)
        resolved_consult_at = resolve_consult_operator_at(
            page_value=new_fm.get("consult_operator_at"),
            canvas_default=canvas_defaults.get("default_consult_operator_at"),
        )
        route_targets = parse_route_targets(new_fm.get("routes"), new_state)
        consult_operator = state_changed and new_state in (resolved_consult_at or [])

        # CANVAS-CROSS-PAGE-INDEX: refresh the reference index for this
        # source BEFORE events fire — the Gardener's on_canvas_event
        # schedules background dispatch that queries the index, and we
        # want that dispatch to see the current post-write state, not
        # the pre-write state.
        broken_refs = await self._update_reference_index_for_write(
            instance_id=instance_id, canvas_id=canvas_id,
            page_slug=page_slug, new_body=new_body,
        )

        # Event emission: distinct event per lifecycle stage.
        if is_new:
            await self._emit(
                instance_id, "canvas.page.created",
                {
                    "canvas_id": canvas_id,
                    "page_path": page_slug,
                    "type": declared_type,
                    "state": new_state,
                    "writer_member_id": writer_member_id,
                },
                member_id=writer_member_id,
            )
        else:
            await self._emit(
                instance_id, "canvas.page.changed",
                {
                    "canvas_id": canvas_id,
                    "page_path": page_slug,
                    "type": declared_type,
                    "state": new_state,
                    "prev_state": prev_state,
                    "writer_member_id": writer_member_id,
                },
                member_id=writer_member_id,
            )
            if state_changed:
                await self._emit(
                    instance_id, "canvas.page.state_changed",
                    {
                        "canvas_id": canvas_id,
                        "page_path": page_slug,
                        "type": declared_type,
                        "prev_state": prev_state,
                        "new_state": new_state,
                        "writer_member_id": writer_member_id,
                    },
                    member_id=writer_member_id,
                )
                if new_state == "archived":
                    await self._emit(
                        instance_id, "canvas.page.archived",
                        {
                            "canvas_id": canvas_id,
                            "page_path": page_slug,
                            "writer_member_id": writer_member_id,
                        },
                        member_id=writer_member_id,
                    )

        return CanvasOpResult(
            ok=True, canvas_id=canvas_id, page_path=page_slug,
            extra={
                "type": declared_type,
                "state": new_state,
                "prev_state": prev_state,
                "state_changed": state_changed,
                "is_new": is_new,
                "type_recognized": type_ok,
                "watchers": list(new_fm.get("watchers") or []),
                "frontmatter": new_fm,
                "route_targets": route_targets,
                "consult_operator_at": resolved_consult_at,
                "consult_operator": consult_operator,
                "broken_references": [
                    {"source": b.source_slug, "target": b.target_slug,
                     "count": b.count, "reason": b.reason}
                    for b in broken_refs
                ],
            },
        )

    # ---- Page list + search ---------------------------------------------

    async def page_list(
        self, *, instance_id: str, canvas_id: str,
    ) -> list[dict]:
        """Return a list of {path, title, type, state, last_updated} entries."""
        root = canvas_dir(self._data_dir, instance_id, canvas_id)
        if not root.is_dir():
            return []
        out: list[dict] = []
        for md_path in sorted(root.rglob("*.md")):
            # Skip version files
            if re.search(r"\.v\d+\.md$", md_path.name):
                continue
            rel = md_path.relative_to(root)
            fm, _ = self._read_page_cached(md_path)
            out.append({
                "path": str(rel).replace(os.sep, "/"),
                "title": fm.get("title", str(rel)),
                "type": fm.get("type", DEFAULT_PAGE_TYPE),
                "state": fm.get("state", ""),
                "last_updated": fm.get("last_updated", ""),
            })
        return out

    async def page_search(
        self,
        *,
        instance_id: str,
        canvas_ids: list[str],
        query: str,
        limit: int = 20,
    ) -> list[dict]:
        """Return pages whose body or title matches ``query``.

        Token-overlap ranked search (PAGE-SEARCH-TOKEN-OVERLAP-V1,
        2026-05-07). Original v1 was strict whole-string substring,
        which produced surprising zero-result misses — e.g. searching
        ``"Canvas docs"`` against pages titled ``concepts/canvas.md``
        returned no hits because the literal phrase wasn't present,
        while ``"canvas"`` did find the page (agent self-identified
        2026-05-07).

        Ranking now combines:
          * Whole-phrase matches (heavily weighted via
            ``_PHRASE_BONUS``) — preserves the prior behavior that
            an exact-phrase match always outranks scattered
            token matches.
          * Per-token substring matches summed — surfaces pages
            that overlap the query semantically even when the
            literal phrase is absent.

        Score breakdown is exposed in the returned ``matches`` field
        for callers that want to introspect ranking.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        tokens = [t for t in re.split(r"\W+", q) if t]
        hits: list[dict] = []
        for canvas_id in canvas_ids:
            root = canvas_dir(self._data_dir, instance_id, canvas_id)
            if not root.is_dir():
                continue
            for md_path in root.rglob("*.md"):
                if re.search(r"\.v\d+\.md$", md_path.name):
                    continue
                fm, body = self._read_page_cached(md_path)
                haystack = (fm.get("title", "") + "\n" + body).lower()
                phrase_count = haystack.count(q) if q else 0
                token_sum = sum(haystack.count(t) for t in tokens)
                if phrase_count == 0 and token_sum == 0:
                    continue
                rel = md_path.relative_to(root)
                hits.append({
                    "canvas_id": canvas_id,
                    "path": str(rel).replace(os.sep, "/"),
                    "title": fm.get("title", str(rel)),
                    # Composite scalar for ergonomic consumption — but
                    # ranking uses the tuple below for strict phrase
                    # dominance.
                    "matches": phrase_count * _PHRASE_BONUS + token_sum,
                    "_phrase_count": phrase_count,
                    "_token_sum": token_sum,
                })
        # Strict phrase dominance: any page with a phrase hit always
        # outranks pages without, regardless of how high the token sum
        # is on the no-phrase pages.
        hits.sort(
            key=lambda h: (h["_phrase_count"], h["_token_sum"]),
            reverse=True,
        )
        for h in hits:
            h.pop("_phrase_count", None)
            h.pop("_token_sum", None)
        return hits[:limit]

    # ---- Canvas list -----------------------------------------------------

    async def list_for_member(
        self, *, member_id: str, include_archived: bool = False,
    ) -> list[dict]:
        canvases = await self._db.list_canvases_for_member(
            member_id=member_id, include_archived=include_archived,
        )
        # Decode pinned_to_spaces JSON column for callers
        for c in canvases:
            raw = c.get("pinned_to_spaces") or ""
            try:
                c["pinned_to_spaces"] = json.loads(raw) if raw else []
            except Exception:
                c["pinned_to_spaces"] = []
        return canvases
