"""Reference primitive — referential self-documentation.

Per REFERENCE-PRIMITIVE-V1. The package owns:

* :mod:`events` — eleven event shapes + the ``"reference"`` source-
  module emitter adapter.
* :mod:`catalog` — :class:`CatalogEntry` dataclass and the SQLite-
  backed :class:`CatalogStore` over ``instance.db``.

Other modules (cohort, ingestion, injection, induction, tools) plug
on top of the catalog and events surfaces. The catalog is the single
runtime query surface; the event stream is for audit + observability.

CC implementation note: ``domain_id`` in the spec maps to the
existing per-space identity (``space_id``) — the architect primer's
"General → Domain → Subdomain" hierarchy is colloquial; the codebase
keys state to ``(instance_id, member_id, space_id)`` and the
reference primitive composes with that. The catalog ``scope`` field
is ``"instance"`` for ``docs/``-derived rows and ``"domain:<space_id>"``
for ``references/``-derived rows."""
