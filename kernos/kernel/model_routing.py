"""Shared chain-and-head resolver for /status, /model, and the
ReasoningService dispatch path (MODEL-AND-STATUS-V1).

The handler renders Models blocks and the runtime dispatcher both
need the same answer to "given a chain and a (member, space)
override, which entries should we iterate, in what order, and what
gets surfaced as the effective head?" This module is the single
source of truth so the read view (handler) and the write effect
(reasoning) can never disagree.

Pure value functions. No I/O. The InstanceDB call to load the
override is the caller's responsibility — keeps this module
side-effect free and testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from kernos.providers.base import ChainConfig, ChainEntry


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectiveChain:
    """The chain entries to iterate for a single dispatch, plus the
    metadata the handler needs to render `/status` and `/model`.

    * ``chain_name``: the chain whose entries are being iterated. May
      be the request's default or an override-driven choice.
    * ``entries``: ordered list of ChainEntry to try. When a head
      override is in effect, the override entry is at index 0 and any
      duplicate is removed from later positions (Codex pre-spec
      refinement #1: "preferred first attempt, not hard pin").
    * ``head_provider`` / ``head_model``: identify the effective head
      that ``/status`` shows.
    * ``override_in_effect``: True iff a chain switch OR head override
      was applied. Distinct from "the row exists" so a row with stale
      fields that resolve to defaults reports False.
    * ``stale_chain_name`` / ``stale_head_spec``: when the persisted
      override referenced a chain or entry that is no longer in the
      current ``ChainConfig``. Surfaces as "(unavailable — not in any
      current chain)" in the handler renderings.
    """
    chain_name: str
    entries: tuple["ChainEntry", ...]
    head_provider: str
    head_model: str
    override_in_effect: bool = False
    stale_chain_name: str | None = None
    stale_head_spec: str | None = None


# ---------------------------------------------------------------------------
# Spec parsing / validation
# ---------------------------------------------------------------------------


def parse_provider_model_spec(spec: str) -> tuple[str, str] | None:
    """Parse 'provider/model' into (provider, model). Returns None when
    the input is not in that exact shape (no slash, empty halves)."""
    if not spec or "/" not in spec:
        return None
    provider, _, model = spec.partition("/")
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        return None
    return provider, model


def list_configured_entries(chains: "ChainConfig") -> list[tuple[str, str]]:
    """Return every (provider_name, model) pair across every chain,
    in chain order then position-within-chain order. Deduplicated.
    Used by `/model` to validate user-supplied head overrides and
    to render the "available entries" rejection message.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for chain_entries in chains.values():
        for entry in chain_entries:
            provider_name = _provider_name(entry)
            pair = (provider_name, entry.model)
            if pair in seen:
                continue
            seen.add(pair)
            out.append(pair)
    return out


def head_spec_in_any_chain(
    chains: "ChainConfig", provider: str, model: str,
) -> bool:
    """True iff (provider, model) matches some entry in some chain.
    /model rejects head overrides not in any built chain so we don't
    need a separate provider/credentials validation path at switch
    time — chain-build already did it at startup."""
    for entry in _iter_all_entries(chains):
        if _provider_name(entry) == provider and entry.model == model:
            return True
    return False


# ---------------------------------------------------------------------------
# Effective-chain resolution (the load-bearing function)
# ---------------------------------------------------------------------------


def resolve_effective_chain(
    *,
    chains: "ChainConfig",
    requested_chain: str,
    override: dict | None,
) -> EffectiveChain:
    """Given the configured chains, the chain the caller asked for,
    and the persisted override (or None), return the
    :class:`EffectiveChain` to iterate.

    Resolution rules (spec section "ReasoningService integration"):

    1. If ``override.chain_name`` is set AND that chain exists, use
       it instead of ``requested_chain``. If set but missing from
       ``chains``, mark it stale and fall back to ``requested_chain``.
    2. If ``override.override_provider`` and ``override_model`` are
       both set AND the (provider, model) is in some configured
       chain, prepend that entry to the chosen chain's entries with
       any duplicate removed from later positions. If set but not in
       any chain, mark it stale and skip the prepend.
    3. The effective head is entries[0]'s provider name and model.
    4. ``override_in_effect`` is True iff a non-stale chain switch
       or non-stale head prepend actually happened.
    """
    chain_name = requested_chain
    stale_chain_name: str | None = None
    stale_head_spec: str | None = None
    chain_was_overridden = False

    if override and override.get("chain_name"):
        candidate_chain = override["chain_name"]
        if candidate_chain in chains:
            chain_name = candidate_chain
            chain_was_overridden = chain_name != requested_chain
        else:
            stale_chain_name = candidate_chain

    base_entries = list(chains.get(chain_name) or chains.get("primary") or [])
    if not base_entries:
        # Defensive: no entries at all. Return a synthetic shell so the
        # caller can render "(no providers configured)" rather than
        # crash. Production chain-build ensures at least one entry.
        return EffectiveChain(
            chain_name=chain_name,
            entries=(),
            head_provider="(none)",
            head_model="(none)",
            override_in_effect=chain_was_overridden,
            stale_chain_name=stale_chain_name,
            stale_head_spec=stale_head_spec,
        )

    head_provider_override = (override or {}).get("override_provider")
    head_model_override = (override or {}).get("override_model")
    head_was_overridden = False

    if head_provider_override and head_model_override:
        source_entry = find_entry_in_any_chain(
            chains, head_provider_override, head_model_override,
        )
        if source_entry is not None:
            base_entries = _prepend_with_dedupe(
                base_entries, source_entry,
                head_provider_override, head_model_override,
            )
            head_was_overridden = True
        else:
            stale_head_spec = (
                f"{head_provider_override}/{head_model_override}"
            )

    head = base_entries[0]
    return EffectiveChain(
        chain_name=chain_name,
        entries=tuple(base_entries),
        head_provider=_provider_name(head),
        head_model=head.model,
        override_in_effect=chain_was_overridden or head_was_overridden,
        stale_chain_name=stale_chain_name,
        stale_head_spec=stale_head_spec,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider_name(entry: "ChainEntry") -> str:
    return getattr(entry.provider, "provider_name", "?")


def _iter_all_entries(chains: "ChainConfig") -> Iterable["ChainEntry"]:
    for chain_entries in chains.values():
        for entry in chain_entries:
            yield entry


def _prepend_with_dedupe(
    base_entries: list["ChainEntry"],
    source_entry: "ChainEntry",
    provider: str,
    model: str,
) -> list["ChainEntry"]:
    """Prepend a sourced override entry to a chain, removing any
    later duplicates (same provider_name + model). The source entry
    may have come from a different chain than the active one — e.g.
    chain_name=primary but the override head is an entry that only
    lives in lightweight. Sourcing-by-spec keeps Provider references
    valid (no synthetic ChainEntry construction).

    Matches by (provider_name, model) rather than ChainEntry
    identity because the same (provider, model) may appear in
    multiple chains.
    """
    deduped = [
        entry for entry in base_entries
        if not (
            _provider_name(entry) == provider and entry.model == model
        )
    ]
    return [source_entry] + deduped


def find_entry_in_any_chain(
    chains: "ChainConfig", provider: str, model: str,
) -> "ChainEntry | None":
    """Locate a ChainEntry matching (provider, model) anywhere in
    chains. Returned to the caller so a head override sourced from a
    DIFFERENT chain than the active one can be prepended to the
    active chain. Returns None when the spec is not in any chain.
    """
    for entry in _iter_all_entries(chains):
        if _provider_name(entry) == provider and entry.model == model:
            return entry
    return None


__all__ = [
    "EffectiveChain",
    "find_entry_in_any_chain",
    "head_spec_in_any_chain",
    "list_configured_entries",
    "parse_provider_model_spec",
    "resolve_effective_chain",
]
