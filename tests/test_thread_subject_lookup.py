"""Subject-specific thread overviews should not lose older matching records."""
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from kernos.kernel.retrieval import RetrievalService


@pytest.fixture
def retrieval():
    compaction = MagicMock()
    compaction.load_thread_evidence = AsyncMock(side_effect=[
        [
            {"status": "open", "label": "open commitment [one]", "text": "Arrange the garden tools [opening sources: log_001/turn_1]"},
            {"status": "open", "label": "open commitment [two]", "text": "Review the orchard schedule [opening sources: log_001/turn_2]"},
            {"status": "open", "label": "open commitment [three]", "text": "Buy paint for the shed [opening sources: log_001/turn_3]"},
            {"status": "open", "label": "open commitment [four]", "text": "Reserve the north workshop room [opening sources: log_001/turn_4]"},
            {"status": "closed", "label": "closed commitment [five]", "text": "Book the north workshop room [opening sources: log_001/turn_5]"},
        ],
        [{"status": "open", "label": "open commitment [six]", "text": "North workshop equipment [opening sources: log_002/turn_1]"}],
    ])
    return RetrievalService(MagicMock(), None, None, compaction, None)


@pytest.mark.asyncio
async def test_subject_overview_ranks_matching_open_records_in_scope(retrieval):
    hits = await retrieval._search_threads("instance", "open commitments for the north workshop room", ["studio", "shared"], "member")
    assert hits[0] == ("Thread state [space: studio; open commitment [four]]:",
                       "Reserve the north workshop room [opening sources: log_001/turn_4]")
    assert any("[six]" in header for header, _ in hits)
    assert all("[five]" not in header for header, _ in hits)
    assert retrieval.compaction.load_thread_evidence.await_args_list == [
        call("instance", "studio", "member"), call("instance", "shared", "member")]


@pytest.mark.asyncio
async def test_bare_overview_returns_newest_three_open_records(retrieval):
    hits = await retrieval._search_threads("instance", "open commitments", ["studio", "shared"], "member")
    assert [header for header, _ in hits] == [
        f"Thread state [space: studio; open commitment [{number}]]:"
        for number in ("one", "two", "three")
    ]
