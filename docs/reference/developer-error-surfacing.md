# Developer Mode Error Surfacing

When developer mode is enabled on a tenant profile (`developer_mode: true`), internal errors that occur between user messages are collected and injected into the next conversation turn as a system block. Both the user and the agent can see and discuss them.

## How It Works

1. An `ErrorBuffer` in the message handler captures WARNING and ERROR log entries from `kernos.*` loggers
2. Entries are stored per-instance in an in-memory buffer (max 20 entries, oldest dropped beyond that)
3. When the next message arrives, if `developer_mode` is on, pending errors are formatted as a `[DEVELOPER: Errors since last message]` block and appended to the system prompt
4. The buffer is cleared after injection — no stale errors accumulate

## What Gets Collected

- All WARNING level log entries from `kernos.*` loggers
- All ERROR level log entries from `kernos.*` loggers
- NOT INFO or DEBUG (those are noise)
- NOT non-kernos loggers (httpx, discord, anthropic SDK internals)

## What the Agent Sees

```
[DEVELOPER: Errors since last message]
WARNING kernos.kernel.covenant_manager: COVENANT_VALIDATE: failed (API status 400: ...)
ERROR kernos.kernel.reasoning: ReasoningRateLimitError: 429 rate limit hit

These are internal system errors visible because developer mode is enabled.
You can discuss them, diagnose them (request_reference or read_source), or ignore them.
[END DEVELOPER]
```

## Developer Mode Off

When developer mode is off (the default), errors are logged to the console as normal but invisible to the conversation. No change in behavior for regular users.

## Enabling Developer Mode

Set `developer_mode: true` on the tenant profile in `data/{instance_id}/state/profile.json`, or through the admin interface when available.
