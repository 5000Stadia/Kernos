"""External event source contracts (WTC v1 C4).

Defines the canonical event shapes that the unified trigger
runtime consumes for external integrations (email observers,
Notion page observers, future webhook receivers). C4 is
contracts-only: the source classes are stubs that document the
payload shape and emit synthetically (so predicates can be tested
against the contract). Real polling / API integrations land in
their own follow-up specs.

Why explicit contracts: predicate authors need a stable selector
language to write rules like "when an email from X arrives,
respond". If the email observer's payload shape is implicit, every
predicate becomes coupled to whichever observer happens to be
running. A documented contract decouples the predicate from the
poller.

Each source documents:

* The ``event_type`` constant.
* The payload shape (required + optional fields).
* Sample selector forms that target it.
* What each field means (so predicate authors don't have to spelunk).

Out of scope for C4:

* Polling logic (IMAP / Gmail API / Notion API client).
* Webhook receivers or the routing into the event_stream.
* Authentication / connection management.
"""
from __future__ import annotations

import logging
from typing import Any

from kernos.kernel.event_stream import emit as _emit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------


# Emitted when an email message is observed in a connected mailbox.
# The observer (future polling / webhook layer) is responsible for
# de-duplicating across polls — once observed, an email message_id
# should not produce a second event with the same message_id.
EVENT_TYPE_EMAIL_MESSAGE_OBSERVED: str = "email.message_observed"


# Emitted when a Notion page is created or updated. ``change_kind``
# in the payload distinguishes "created" from "updated" so
# predicates can target either or both via the predicate AST.
EVENT_TYPE_NOTION_PAGE_OBSERVED: str = "notion.page_observed"


# ---------------------------------------------------------------------------
# EmailMessageSource — email.message_observed
# ---------------------------------------------------------------------------


class EmailMessageSource:
    """Emits ``email.message_observed`` events into the durable
    stream.

    Payload contract:

    .. code-block:: python

        {
            "message_id": str,        # provider-stable id (de-dup key)
            "thread_id": str,         # may be empty if provider doesn't surface threads
            "from_address": str,      # canonical "user@host" form
            "from_name": str,         # display name; "" if not present
            "to_addresses": list[str],
            "cc_addresses": list[str],
            "subject": str,
            "received_iso": str,      # ISO 8601 UTC
            "snippet": str,           # short preview; observers SHOULD truncate (<=512 chars)
            "labels": list[str],      # provider labels (e.g., Gmail labels)
            "mailbox": str,           # observer-supplied identifier of the mailbox
        }

    Selector samples:

    * ``{"op": "eq", "path": "event_type", "value": "email.message_observed"}``
      — fire on any observed email.
    * ``{"op": "eq", "path": "payload.from_address", "value": "kit@anthropic.com"}``
      — fire only on emails from a specific sender.
    * ``{"op": "AND", "operands": [...]}`` — combine event_type with
      payload-shape clauses (see :mod:`kernos.kernel.workflows.predicates`
      for the full AST grammar).

    Temporal-relation semantics for email:

    * ``on(Y)`` — fire as soon as the email is observed.
    * ``before(Y, N)`` and ``after(Y, N)`` are valid syntactically
      but anchor to ``event.timestamp`` (observation time), not the
      provider's ``received_iso``. C6 may extend this for sources
      whose payload carries an authoritative time anchor.
    """

    name = "email_message_source"

    def __init__(self, *, instance_id: str) -> None:
        self._instance_id = instance_id
        self._started: bool = False

    async def start(self) -> None:
        """v1 (C4) is a stub — real polling/webhook receivers
        attach to this source in their own follow-up specs."""
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def emit_observed(
        self,
        *,
        message_id: str,
        from_address: str = "",
        thread_id: str = "",
        from_name: str = "",
        to_addresses: list[str] | None = None,
        cc_addresses: list[str] | None = None,
        subject: str = "",
        received_iso: str = "",
        snippet: str = "",
        labels: list[str] | None = None,
        mailbox: str = "",
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Emit a single ``email.message_observed`` event. Returns
        the substrate-generated event_id.

        ``message_id`` is required — it's the provider-stable
        de-duplication key. Observers must not call this twice for
        the same ``message_id`` (the trigger runtime relies on
        that contract; the predicates layer doesn't re-dedup
        observed emails).
        """
        if not message_id:
            raise ValueError("message_id is required for email.message_observed")
        payload: dict[str, Any] = {
            "message_id": message_id,
            "thread_id": thread_id,
            "from_address": from_address,
            "from_name": from_name,
            "to_addresses": list(to_addresses or []),
            "cc_addresses": list(cc_addresses or []),
            "subject": subject,
            "received_iso": received_iso,
            "snippet": snippet,
            "labels": list(labels or []),
            "mailbox": mailbox,
        }
        if extra:
            payload.update(extra)
        return await _emit(
            instance_id=self._instance_id,
            event_type=EVENT_TYPE_EMAIL_MESSAGE_OBSERVED,
            payload=payload,
        )


# ---------------------------------------------------------------------------
# NotionPageSource — notion.page_observed
# ---------------------------------------------------------------------------


class NotionPageSource:
    """Emits ``notion.page_observed`` events into the durable
    stream.

    Payload contract:

    .. code-block:: python

        {
            "page_id": str,           # Notion page id (de-dup key in combination with change_kind)
            "change_kind": str,       # "created" | "updated"
            "title": str,
            "parent_kind": str,       # "database" | "page" | "workspace"
            "parent_id": str,         # parent database/page id; "" for workspace
            "url": str,               # canonical page URL
            "last_edited_iso": str,   # ISO 8601 UTC
            "last_edited_by": str,    # Notion user id; "" if unavailable
            "tags": list[str],        # property values surfaced as tags (database pages)
        }

    Selector samples:

    * ``{"op": "eq", "path": "event_type", "value": "notion.page_observed"}``
      — fire on any observed Notion page change.
    * ``{"op": "eq", "path": "payload.change_kind", "value": "created"}``
      — only fire on new page creations.
    * ``{"op": "eq", "path": "payload.parent_id", "value": "<db-id>"}``
      — fire only on pages within a specific database.

    Temporal-relation semantics for Notion:

    * ``on(Y)`` — fire as soon as the change is observed.
    * For ``updated`` pages, the observer SHOULD only emit when
      ``last_edited_iso`` advances; otherwise repeated polls
      produce noise. v1 doesn't enforce this — it's part of the
      observer's own contract.
    """

    name = "notion_page_source"

    def __init__(self, *, instance_id: str) -> None:
        self._instance_id = instance_id
        self._started: bool = False

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def emit_observed(
        self,
        *,
        page_id: str,
        change_kind: str,
        title: str = "",
        parent_kind: str = "",
        parent_id: str = "",
        url: str = "",
        last_edited_iso: str = "",
        last_edited_by: str = "",
        tags: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Emit a single ``notion.page_observed`` event. Returns
        the substrate-generated event_id.

        ``page_id`` and ``change_kind`` are required.
        ``change_kind`` must be ``"created"`` or ``"updated"``;
        anything else raises ``ValueError`` so a misbehaving
        observer can't silently produce events with an unknown
        kind that no predicate can target.
        """
        if not page_id:
            raise ValueError("page_id is required for notion.page_observed")
        if change_kind not in ("created", "updated"):
            raise ValueError(
                f"change_kind must be 'created' or 'updated', got "
                f"{change_kind!r}"
            )
        payload: dict[str, Any] = {
            "page_id": page_id,
            "change_kind": change_kind,
            "title": title,
            "parent_kind": parent_kind,
            "parent_id": parent_id,
            "url": url,
            "last_edited_iso": last_edited_iso,
            "last_edited_by": last_edited_by,
            "tags": list(tags or []),
        }
        if extra:
            payload.update(extra)
        return await _emit(
            instance_id=self._instance_id,
            event_type=EVENT_TYPE_NOTION_PAGE_OBSERVED,
            payload=payload,
        )


__all__ = [
    "EVENT_TYPE_EMAIL_MESSAGE_OBSERVED",
    "EVENT_TYPE_NOTION_PAGE_OBSERVED",
    "EmailMessageSource",
    "NotionPageSource",
]
