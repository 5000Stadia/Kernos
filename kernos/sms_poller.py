"""SMS Poller — polls Twilio for inbound SMS on an interval.

Runs inside the Discord bot process. No webhook, no public server needed.
"""
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class SMSPoller:
    """Polls Twilio for inbound SMS messages on an interval."""

    def __init__(
        self,
        adapter,            # TwilioSMSAdapter
        handler,            # MessageHandler
        account_sid: str,
        auth_token: str,
        twilio_number: str,
        interval: float = 30.0,
    ) -> None:
        self._adapter = adapter
        self._handler = handler
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._twilio_number = twilio_number
        self._interval = interval
        self._processed_sids: set[str] = set()
        self._last_check = datetime.now(timezone.utc)
        self._task: asyncio.Task | None = None
        # CONNECTION_POOL_LEAK fix (2026-06-08): own ONE Twilio client and
        # reuse it. Constructing a new TwilioClient every poll (every few
        # seconds) created a fresh internal requests.Session that was never
        # closed — its keepalive connections piled up in CLOSE_WAIT (the
        # week-long GatewayHealthObserver alert; live sockets confirmed Twilio).
        self._twilio_client = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "SMS_POLL: started interval=%.1fs number=%s",
            self._interval, self._twilio_number,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._close_twilio_client()

    def _get_twilio_client(self):
        """Lazily create + reuse ONE Twilio client (its internal
        requests.Session is pooled and kept alive across polls)."""
        if self._twilio_client is None:
            from twilio.rest import Client as TwilioClient
            self._twilio_client = TwilioClient(self._account_sid, self._auth_token)
        return self._twilio_client

    def _close_twilio_client(self) -> None:
        c = self._twilio_client
        self._twilio_client = None
        if c is not None:
            try:
                c.http_client.session.close()
            except Exception:
                pass

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._check_messages()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("SMS_POLL: error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _check_messages(self) -> None:
        # Silence Twilio/HTTP library noise during routine polling
        _noisy_loggers = ["twilio", "urllib3", "httpx"]
        _saved = {n: logging.getLogger(n).level for n in _noisy_loggers}
        for n in _noisy_loggers:
            logging.getLogger(n).setLevel(logging.ERROR)

        try:
            twilio_client = self._get_twilio_client()

            # Fetch recent inbound messages (sync — run in thread)
            messages = await asyncio.to_thread(
                twilio_client.messages.list,
                to=self._twilio_number,
                date_sent_after=self._last_check,
            )
        finally:
            # Restore logger levels after the Twilio API call
            for n, level in _saved.items():
                logging.getLogger(n).setLevel(level)

        for msg in messages:
            if msg.sid in self._processed_sids:
                continue
            if msg.direction != "inbound":
                continue

            self._processed_sids.add(msg.sid)

            # Check authorization
            if not self._adapter.is_authorized(msg.from_):
                logger.warning("SMS_POLL: unauthorized number %s", msg.from_)
                try:
                    await asyncio.to_thread(
                        twilio_client.messages.create,
                        body="This number is not authorized.",
                        from_=self._twilio_number,
                        to=msg.from_,
                    )
                except Exception as exc:
                    logger.warning("SMS_POLL: failed to send rejection: %s", exc)
                continue

            logger.info("SMS_POLL: inbound from=%s body=%r", msg.from_, msg.body[:80])

            # Build NormalizedMessage via adapter
            raw = {
                "From": msg.from_,
                "To": self._twilio_number,
                "Body": msg.body,
                "SmsSid": msg.sid,
            }
            normalized = self._adapter.inbound(raw)

            # Process through handler
            try:
                response_text = await self._handler.process(normalized)
            except Exception as exc:
                logger.error("SMS_POLL: handler failed for %s: %s", msg.from_, exc)
                response_text = "Sorry, something went wrong processing your message."

            # Send reply via REST API
            await self._adapter.send_outbound(
                instance_id=normalized.instance_id,
                channel_target=msg.from_,
                message=response_text,
            )

        self._last_check = datetime.now(timezone.utc)

        # Prevent processed_sids from growing forever
        if len(self._processed_sids) > 1000:
            self._processed_sids = set(list(self._processed_sids)[-500:])
