# Security review

## Scope

The server performs one external side effect: sending plain text to one configured
Telegram chat through `sendMessage`.

## Controls

- Bot token and destination are process configuration, never model arguments.
- Destination is restricted to a numeric chat ID.
- Token format is validated before it is interpolated into the HTTPS endpoint.
- Errors never contain the request URL, token, response description, or message text.
- Telegram responses are capped at 1 MB before JSON parsing.
- Messages are plain text, limited to 4096 characters, and have link previews disabled.
- `protect_content` defaults to true.
- SQLite reservations reduce concurrent and recurring duplicate sends.
- Same event ID with changed text fails closed.
- Failed confirmed requests release their reservation for a later retry.
- The MCP tool is annotated as an external write and open-world operation.

## Residual risks

- Telegram Bot API has no idempotency key for `sendMessage`. A timeout after Telegram
  accepted a message but before the response arrived can produce a later duplicate.
- Anyone who obtains the BotFather token can control the bot. Production deployments
  should inject it from a secret manager and rotate it after any suspected exposure.
- Telegram receives the alert text. The workflow should minimize personal, medical,
  financial, and other sensitive content before calling `send_alert`.
