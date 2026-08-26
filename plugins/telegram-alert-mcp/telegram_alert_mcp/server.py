"""A deliberately small MCP server for Telegram workflow alerts.

The server exposes one external-write tool, ``send_alert``. The destination chat
is fixed in process configuration, so a model cannot choose an arbitrary recipient.
Successful deliveries are recorded by caller-supplied event ID to suppress repeats
when a scheduled workflow sees the same source event again.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

BOT_API_ROOT = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_PENDING_LEASE_SECONDS = 300
MAX_MESSAGE_LENGTH = 4096
MAX_EVENT_ID_LENGTH = 256
MAX_RESPONSE_BYTES = 1_000_000
_BOT_TOKEN = re.compile(r"^[1-9][0-9]{4,15}:[A-Za-z0-9_-]{20,}$")
_CHAT_ID = re.compile(r"^-?[1-9][0-9]{0,19}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

EXTERNAL_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

mcp = FastMCP(
    "telegram-alert-mcp",
    instructions=(
        "Use send_alert only after the workflow has verified a real alert condition. "
        "Every call sends data to one preconfigured Telegram chat. Reuse the stable "
        "source event ID on later cycles so duplicate alerts are suppressed."
    ),
)


class TelegramAlertError(RuntimeError):
    """A safe error suitable for returning through MCP."""


def _parse_bool(value: str, field: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise TelegramAlertError(f"{field} must be true or false.")


def _require_bot_token(value: str) -> str:
    if not _BOT_TOKEN.fullmatch(value):
        raise TelegramAlertError("TELEGRAM_BOT_TOKEN is missing or invalid.")
    return value


def _require_chat_id(value: str) -> str:
    if not _CHAT_ID.fullmatch(value):
        raise TelegramAlertError("TELEGRAM_CHAT_ID must be a numeric Telegram chat ID.")
    return value


def _require_message(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TelegramAlertError("message must not be empty.")
    if len(value) > MAX_MESSAGE_LENGTH:
        raise TelegramAlertError(f"message must be at most {MAX_MESSAGE_LENGTH} characters.")
    if "\x00" in value:
        raise TelegramAlertError("message contains a forbidden NUL character.")
    return value


def _require_event_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_EVENT_ID_LENGTH
        or _CONTROL.search(value)
    ):
        raise TelegramAlertError(
            f"event_id must be 1-{MAX_EVENT_ID_LENGTH} printable characters."
        )
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    chat_id: str
    state_path: Path
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    protect_content: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = _require_bot_token(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        chat_id = _require_chat_id(os.environ.get("TELEGRAM_CHAT_ID", ""))
        raw_timeout = os.environ.get(
            "TELEGRAM_ALERT_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)
        )
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise TelegramAlertError(
                "TELEGRAM_ALERT_TIMEOUT_SECONDS must be a number."
            ) from exc
        if not 1 <= timeout <= 60:
            raise TelegramAlertError(
                "TELEGRAM_ALERT_TIMEOUT_SECONDS must be between 1 and 60."
            )
        default_path = Path.home() / ".local" / "state" / "telegram-alert-mcp" / "alerts.sqlite3"
        state_path = Path(
            os.environ.get("TELEGRAM_ALERT_STATE_PATH", str(default_path))
        ).expanduser()
        if state_path.exists() and state_path.is_dir():
            raise TelegramAlertError("TELEGRAM_ALERT_STATE_PATH must be a file path.")
        protect_content = _parse_bool(
            os.environ.get("TELEGRAM_ALERT_PROTECT_CONTENT", "true"),
            "TELEGRAM_ALERT_PROTECT_CONTENT",
        )
        return cls(
            bot_token=bot_token,
            chat_id=chat_id,
            state_path=state_path,
            timeout_seconds=timeout,
            protect_content=protect_content,
        )


@dataclass(frozen=True)
class Reservation:
    outcome: str
    telegram_message_id: int | None = None
    delivered_at: str | None = None


class DeliveryStore:
    """SQLite-backed duplicate suppression for recurring workflows."""

    def __init__(self, path: Path, lease_seconds: int = DEFAULT_PENDING_LEASE_SECONDS):
        self.path = path
        self.lease = timedelta(seconds=lease_seconds)

    def _connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_id TEXT PRIMARY KEY,
                    message_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'delivered')),
                    reserved_at TEXT NOT NULL,
                    delivered_at TEXT,
                    telegram_message_id INTEGER
                )
                """
            )
            return connection
        except (OSError, sqlite3.Error) as exc:
            raise TelegramAlertError("Could not open the local alert delivery state.") from exc

    def reserve(self, event_id: str, message_sha256: str) -> Reservation:
        now = datetime.now(UTC)
        now_text = now.isoformat()
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT * FROM deliveries WHERE event_id = ?", (event_id,)
                    ).fetchone()
                    if row is None:
                        connection.execute(
                            """INSERT INTO deliveries
                               (event_id, message_sha256, status, reserved_at)
                               VALUES (?, ?, 'pending', ?)""",
                            (event_id, message_sha256, now_text),
                        )
                        return Reservation("reserved")
                    if row["message_sha256"] != message_sha256:
                        raise TelegramAlertError(
                            "event_id was already used with different alert text."
                        )
                    if row["status"] == "delivered":
                        return Reservation(
                            "duplicate",
                            telegram_message_id=row["telegram_message_id"],
                            delivered_at=row["delivered_at"],
                        )
                    reserved_at = datetime.fromisoformat(row["reserved_at"])
                    if now - reserved_at < self.lease:
                        return Reservation("in_progress")
                    connection.execute(
                        "UPDATE deliveries SET reserved_at = ? WHERE event_id = ?",
                        (now_text, event_id),
                    )
                    return Reservation("reserved")
        except TelegramAlertError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise TelegramAlertError("Could not update the local alert delivery state.") from exc

    def mark_delivered(
        self, event_id: str, message_sha256: str, telegram_message_id: int
    ) -> str:
        delivered_at = datetime.now(UTC).isoformat()
        try:
            with closing(self._connect()) as connection:
                with connection:
                    updated = connection.execute(
                        """UPDATE deliveries
                           SET status = 'delivered', delivered_at = ?, telegram_message_id = ?
                           WHERE event_id = ? AND message_sha256 = ? AND status = 'pending'""",
                        (delivered_at, telegram_message_id, event_id, message_sha256),
                    ).rowcount
                    if updated != 1:
                        raise TelegramAlertError(
                            "Could not finalize the alert delivery record."
                        )
                    return delivered_at
        except TelegramAlertError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TelegramAlertError("Could not update the local alert delivery state.") from exc

    def release(self, event_id: str, message_sha256: str) -> None:
        """Allow the next workflow cycle to retry a confirmed failed request."""
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        """DELETE FROM deliveries
                           WHERE event_id = ? AND message_sha256 = ? AND status = 'pending'""",
                        (event_id, message_sha256),
                    )
        except (OSError, sqlite3.Error):
            # Preserve the original Telegram error. A pending lease expires automatically.
            return


UrlOpen = Callable[..., Any]


class TelegramBotClient:
    def __init__(self, settings: Settings, urlopen: UrlOpen = urllib.request.urlopen):
        self.settings = settings
        self.urlopen = urlopen

    def send_message(self, message: str) -> int:
        payload = {
            "chat_id": self.settings.chat_id,
            "text": message,
            "protect_content": self.settings.protect_content,
            "link_preview_options": {"is_disabled": True},
        }
        request = urllib.request.Request(
            f"{BOT_API_ROOT}/bot{self.settings.bot_token}/sendMessage",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "telegram-alert-mcp/0.2.0",
            },
            method="POST",
        )
        try:
            with self.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise TelegramAlertError("Telegram returned an oversized response.")
        except urllib.error.HTTPError as exc:
            retry_after: int | None = None
            try:
                parsed_error = json.loads(exc.read(MAX_RESPONSE_BYTES).decode("utf-8"))
                raw_retry = parsed_error.get("parameters", {}).get("retry_after")
                if isinstance(raw_retry, int):
                    retry_after = raw_retry
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            suffix = f"; retry after {retry_after} seconds" if retry_after else ""
            raise TelegramAlertError(
                f"Telegram Bot API rejected the alert (HTTP {exc.code}{suffix})."
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TelegramAlertError(
                "Could not reach Telegram Bot API; the workflow may retry this event."
            ) from None

        try:
            parsed = json.loads(body.decode("utf-8"))
            if parsed.get("ok") is not True:
                raise TelegramAlertError("Telegram Bot API did not accept the alert.")
            message_id = parsed["result"]["message_id"]
            if not isinstance(message_id, int):
                raise TypeError
            return message_id
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if isinstance(exc, TelegramAlertError):
                raise
            raise TelegramAlertError("Telegram returned an invalid response.") from None


def _message_digest(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _send_alert(
    message: str,
    event_id: str,
    *,
    settings: Settings | None = None,
    store: DeliveryStore | None = None,
    client: TelegramBotClient | None = None,
) -> dict[str, Any]:
    message = _require_message(message)
    event_id = _require_event_id(event_id)
    settings = settings or Settings.from_env()
    digest = _message_digest(message)
    store = store or DeliveryStore(settings.state_path)
    reservation = store.reserve(event_id, digest)
    if reservation.outcome == "duplicate":
        return {
            "status": "duplicate_suppressed",
            "event_id": event_id,
            "telegram_message_id": reservation.telegram_message_id,
            "delivered_at": reservation.delivered_at,
        }
    if reservation.outcome == "in_progress":
        return {"status": "delivery_in_progress", "event_id": event_id}

    client = client or TelegramBotClient(settings)
    try:
        telegram_message_id = client.send_message(message)
    except Exception:
        store.release(event_id, digest)
        raise
    delivered_at = store.mark_delivered(event_id, digest, telegram_message_id)
    return {
        "status": "sent",
        "event_id": event_id,
        "telegram_message_id": telegram_message_id,
        "delivered_at": delivered_at,
    }


@mcp.tool(
    title="Send Telegram alert",
    annotations=EXTERNAL_WRITE,
)
def send_alert(message: str, event_id: str) -> dict[str, Any]:
    """Send one confirmed alert to the configured Telegram chat.

    This is an external side effect. Call it only after the source data and alert
    condition have been checked. ``event_id`` must be a stable unique identifier
    derived from the source event, such as ``gmail:<message-id>:rule-v1``. Reusing
    that ID with the same text suppresses duplicate alerts in later schedule cycles.
    """
    return _send_alert(message, event_id)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
