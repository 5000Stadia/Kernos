import logging
import os
from datetime import datetime, timezone

from twilio.twiml.messaging_response import MessagingResponse

from kernos.messages.adapters.base import BaseAdapter
from kernos.messages.models import AuthLevel, NormalizedMessage

# SMS supports text and MMS (images/media); list both per Blueprint capability model.
SMS_CAPABILITIES = ["text", "mms"]

_SMS_LIMIT = 1600
_SMS_CHUNK = 1550
_MORE_SUFFIX = " [...] Reply MORE for the rest."

logger = logging.getLogger(__name__)


class TwilioSMSAdapter(BaseAdapter):
    """
    Translates between Twilio SMS webhook payloads and NormalizedMessage.

    Knows about Twilio. Knows nothing about the handler or the kernel.

    Twilio webhook fields used:
        From    — sender's E.164 phone number
        To      — our Twilio number (used for instance routing once multi-instance)
        Body    — message text
        SmsSid  — per-message Twilio identifier
    """

    def __init__(self) -> None:
        self._owner_phone = os.getenv("OWNER_PHONE_NUMBER", "")
        self._overflow: dict[str, str] = {}  # conversation_id → remaining text

        # Outbound credentials
        self._account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self._auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self._from_number = os.getenv("TWILIO_PHONE_NUMBER", "")
        # CONNECTION_POOL_LEAK fix (2026-06-08): reuse ONE Twilio client so we
        # don't open a fresh, never-closed requests.Session per outbound send.
        self._twilio_client = None

        # Authorized numbers — comma-separated list. Replaces single OWNER_PHONE_NUMBER for auth.
        authorized_raw = os.getenv("AUTHORIZED_NUMBERS", "")
        self._authorized_numbers: set[str] = set()
        if authorized_raw:
            self._authorized_numbers = {n.strip() for n in authorized_raw.split(",") if n.strip()}
        # Always include owner phone if set
        if self._owner_phone:
            self._authorized_numbers.add(self._owner_phone)

    def is_authorized(self, phone_number: str) -> bool:
        """Check if a phone number is authorized to interact."""
        if not self._authorized_numbers:
            # No authorization list configured — allow all (backward compat)
            return True
        return phone_number in self._authorized_numbers

    def inbound(self, raw_request: dict) -> NormalizedMessage:
        """Translate a Twilio webhook form payload into a NormalizedMessage."""
        sender = raw_request.get("From", "")
        body = raw_request.get("Body", "").strip()

        # Phone number is identification, not authentication — per Blueprint.
        # A match gives owner_unverified, not owner_verified.
        auth_level = (
            AuthLevel.owner_unverified
            if sender and sender == self._owner_phone
            else AuthLevel.unknown
        )

        # Instance identity: KERNOS_INSTANCE_ID overrides per-adapter derivation
        instance_id = os.getenv("KERNOS_INSTANCE_ID") or self._owner_phone

        # conversation_id is per-sender — SMS has no thread concept beyond who's talking.
        conversation_id = sender

        return NormalizedMessage(
            content=body,
            sender=sender,
            sender_auth_level=auth_level,
            platform="sms",
            platform_capabilities=SMS_CAPABILITIES,
            conversation_id=conversation_id,
            timestamp=datetime.now(timezone.utc),
            instance_id=instance_id,
        )

    def outbound(self, response: str, original_message: NormalizedMessage) -> str:
        """Return a TwiML string ready to send as an HTTP response to Twilio."""
        conversation_id = original_message.conversation_id

        # If this was a MORE request and overflow exists, serve the next chunk
        # instead of the handler's response (which was generated unnecessarily).
        if original_message.content.strip().upper() == "MORE" and conversation_id in self._overflow:
            remaining = self._overflow.pop(conversation_id)
            if len(remaining) > _SMS_LIMIT:
                text = remaining[:_SMS_CHUNK] + _MORE_SUFFIX
                self._overflow[conversation_id] = remaining[_SMS_CHUNK:]
            else:
                text = remaining
        elif len(response) > _SMS_LIMIT:
            text = response[:_SMS_CHUNK] + _MORE_SUFFIX
            self._overflow[conversation_id] = response[_SMS_CHUNK:]
        else:
            text = response

        resp = MessagingResponse()
        resp.message(text)
        return str(resp)

    async def send_outbound(self, instance_id: str, channel_target: str, message: str) -> bool:
        """Send an outbound SMS via Twilio REST API."""
        if not self._account_sid or not self._auth_token or not self._from_number:
            logger.warning("OUTBOUND: sms send failed — Twilio credentials not configured")
            return False
        try:
            import asyncio
            if self._twilio_client is None:
                from twilio.rest import Client
                self._twilio_client = Client(self._account_sid, self._auth_token)
            twilio_client = self._twilio_client
            # Twilio client is sync — run in thread to avoid blocking
            await asyncio.to_thread(
                twilio_client.messages.create,
                body=message,
                from_=self._from_number,
                to=channel_target,
            )
            logger.info(
                "OUTBOUND: channel=sms target=%s instance=%s length=%d success=True",
                channel_target, instance_id, len(message),
            )
            return True
        except Exception as exc:
            logger.warning(
                "OUTBOUND: channel=sms target=%s instance=%s success=False error=%s",
                channel_target, instance_id, exc,
            )
            return False

    @property
    def can_send_outbound(self) -> bool:
        return bool(self._account_sid and self._auth_token and self._from_number)
