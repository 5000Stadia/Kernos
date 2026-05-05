"""Kernel tool schemas and helpers.

Tool schemas are JSON dicts defining the tool's name, description, and input_schema.
Pure helper functions (_read_source) are co-located with their schemas.
Tool HANDLERS remain in ReasoningService for now (they need the full service context).

REFERENCE-PRIMITIVE-V1: read_doc retired here. Canonical documentation
now reaches via ``request_reference`` — see kernos/kernel/reference/.
"""
from kernos.kernel.tools.schemas import (
    CANVAS_CREATE_TOOL,
    CANVAS_LIST_TOOL,
    CANVAS_PREFERENCE_CONFIRM_TOOL,
    CANVAS_PREFERENCE_EXTRACT_TOOL,
    DIAGNOSE_LLM_CHAIN_TOOL,
    DIAGNOSE_MESSENGER_TOOL,
    INSPECT_PARCEL_TOOL,
    INSPECT_STATE_TOOL,
    LIST_PARCELS_TOOL,
    MANAGE_CAPABILITIES_TOOL,
    PACK_PARCEL_TOOL,
    PAGE_LIST_TOOL,
    PAGE_READ_TOOL,
    PAGE_SEARCH_TOOL,
    PAGE_WRITE_TOOL,
    READ_SOURCE_TOOL,
    READ_SOUL_TOOL,
    REMEMBER_DETAILS_TOOL,
    REQUEST_TOOL,
    RESPOND_TO_PARCEL_TOOL,
    SET_CHAIN_MODEL_TOOL,
    UPDATE_SOUL_TOOL,
    SOUL_UPDATABLE_FIELDS,
    read_source,
)

__all__ = [
    "CANVAS_CREATE_TOOL",
    "CANVAS_LIST_TOOL",
    "CANVAS_PREFERENCE_CONFIRM_TOOL",
    "CANVAS_PREFERENCE_EXTRACT_TOOL",
    "DIAGNOSE_LLM_CHAIN_TOOL",
    "DIAGNOSE_MESSENGER_TOOL",
    "INSPECT_PARCEL_TOOL",
    "INSPECT_STATE_TOOL",
    "LIST_PARCELS_TOOL",
    "MANAGE_CAPABILITIES_TOOL",
    "PACK_PARCEL_TOOL",
    "PAGE_LIST_TOOL",
    "PAGE_READ_TOOL",
    "PAGE_SEARCH_TOOL",
    "PAGE_WRITE_TOOL",
    "READ_SOURCE_TOOL",
    "READ_SOUL_TOOL",
    "REMEMBER_DETAILS_TOOL",
    "REQUEST_TOOL",
    "RESPOND_TO_PARCEL_TOOL",
    "SET_CHAIN_MODEL_TOOL",
    "UPDATE_SOUL_TOOL",
    "SOUL_UPDATABLE_FIELDS",
    "read_source",
]
