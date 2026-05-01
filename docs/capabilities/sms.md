# SMS (Twilio)

Bidirectional SMS via Twilio. A2P registration approved. Both inbound (user texts Kernos) and outbound (Kernos texts user) are supported.

## Status

Live. No public server required — inbound SMS uses polling, not webhooks.

## How It Works

**Inbound:** The `SMSPoller` runs inside the Discord bot process, checking Twilio every 5 seconds for new messages. When an SMS arrives, it's normalized, processed through the handler (same as Discord messages), and the response is sent back via Twilio REST API.

**Outbound:** `send_outbound()` on the Twilio adapter sends SMS via the REST API. Used by `handler.send_outbound()` for kernel-initiated messages.

## Setup

Add to `.env`:
```
# Instance identity — same instance across Discord + SMS
KERNOS_INSTANCE_ID=discord:000000000000000000

# Twilio credentials
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token_here
TWILIO_PHONE_NUMBER=+1XXXXXXXXXX
OWNER_PHONE_NUMBER=+1XXXXXXXXXX
AUTHORIZED_NUMBERS=+1XXXXXXXXXX,+1YYYYYYYYYY
KERNOS_SMS_POLL_INTERVAL=30         # optional, default 30 seconds
```

No webhook URL, no public server, no ngrok needed.

## Phone Authorization

Only numbers in `AUTHORIZED_NUMBERS` (plus the owner number) can interact. Unauthorized numbers receive "This number is not authorized."

## Channel Status

Appears in `manage_channels list` as "Twilio SMS" with `can_send_outbound = True`.

## Code Locations

| Component | Path |
|-----------|------|
| TwilioSMSAdapter | `kernos/messages/adapters/twilio_sms.py` |
| SMSPoller | `kernos/sms_poller.py` |
| Startup wiring | `kernos/server.py` (on_ready) |
| Webhook path (cloud) | `kernos/app.py` |
