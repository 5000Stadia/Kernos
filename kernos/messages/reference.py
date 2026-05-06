"""Thin index for Kernos documentation.

The full docs live in docs/ and are accessed via the reference
primitive (REFERENCE-PRIMITIVE-V1). This module provides the
DOCS_HINT for the system prompt and any always-in-prompt items too
small for their own doc file.
"""

DOCS_HINT = """\
Your canonical documentation reaches via request_reference(brief). \
Ask for what you need ("how does the gate work", "what's a covenant", \
etc.) and the reference primitive surfaces the matching section. \
For a navigation map, start with \
request_reference('kernos documentation map') — that finds \
docs/index.md, which lists everything available across architecture, \
capabilities, behaviors, identity, and roadmap."""
