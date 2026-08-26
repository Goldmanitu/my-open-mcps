# Privacy

## Data flow

This plugin has no developer-operated backend. When `send_alert` is approved and
called, the configured alert text is sent directly from the user's machine to the
official Telegram Bot API and then to the preconfigured Telegram chat.

The developer does not receive the alert, BotFather token, chat ID, mail content, or
delivery history.

## Local data

The plugin stores a local SQLite delivery record containing:

- the caller-provided event ID;
- a SHA-256 digest of the alert text;
- Telegram's numeric message ID;
- reservation and delivery timestamps.

The database does not store the BotFather token, chat ID, or alert text. Its default
location is under the current user's local state directory and can be overridden with
`TELEGRAM_ALERT_STATE_PATH`.

## Credentials

On macOS, the launcher can read the token and chat ID from the user's Keychain service
`telegram-alert-mcp`. On other systems they may be supplied through the MCP process
environment. Credentials are used only to call Telegram Bot API and are not included
in MCP results or safe error messages.

## User responsibility

Telegram receives every approved alert. Users should minimize personal, medical,
financial, legal, confidential, and regulated information and review Telegram's own
privacy terms before enabling a production workflow.
