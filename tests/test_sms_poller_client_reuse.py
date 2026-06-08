"""CONNECTION_POOL_LEAK fix (2026-06-08): the SMS poller created a new
TwilioClient (with an unclosed internal requests.Session) on every poll —
CLOSE_WAIT sockets to *.twilio.com piled up (the week-long observer alert,
live-confirmed by Codex). It now owns ONE client and reuses it.
"""
from unittest.mock import MagicMock

from kernos.sms_poller import SMSPoller


def _poller():
    return SMSPoller(
        adapter=MagicMock(), handler=MagicMock(),
        account_sid="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        auth_token="tok", twilio_number="+15555550100",
    )


def test_reuses_one_twilio_client():
    p = _poller()
    c1 = p._get_twilio_client()
    c2 = p._get_twilio_client()
    assert c1 is c2  # reused across polls, not recreated


def test_close_clears_client():
    p = _poller()
    p._get_twilio_client()
    assert p._twilio_client is not None
    p._close_twilio_client()  # must not raise
    assert p._twilio_client is None


def test_close_safe_when_never_created():
    _poller()._close_twilio_client()  # no client yet → no error
