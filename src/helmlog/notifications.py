"""Notification system for threaded comments Phase 2 (#284).

Handles @mention parsing, notification creation, and pluggable channels
(platform + email).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# @mention parsing
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"@(\w[\w.-]*)")


def parse_mentions(body: str) -> list[str]:
    """Extract @username tokens from a comment body.

    Returns a list of unique usernames (without the @ prefix).
    """
    return list(dict.fromkeys(_MENTION_RE.findall(body)))


def render_mentions_html(body: str, user_map: dict[str, int]) -> str:
    """Replace @username with styled HTML links.

    Only usernames found in *user_map* are linked; unknown @mentions are
    left as plain text.
    """

    def _replace(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in user_map:
            uid = user_map[name]
            return f'<a class="mention" data-user-id="{uid}">@{name}</a>'
        return m.group(0)

    return _MENTION_RE.sub(_replace, body)


# ---------------------------------------------------------------------------
# Notification creation helpers
# ---------------------------------------------------------------------------


async def notify_mention(
    storage: Storage,
    comment_id: int,
    thread_id: int,
    session_id: int,
    actor_id: int,
    mentioned_user_ids: list[int],
) -> int:
    """Create mention notifications for each mentioned user (except actor)."""
    count = 0
    for uid in mentioned_user_ids:
        if uid == actor_id:
            continue
        await storage.create_notification(
            uid,
            "mention",
            source_thread_id=thread_id,
            source_comment_id=comment_id,
            session_id=session_id,
            actor_id=actor_id,
            message="mentioned you in a comment",
        )
        count += 1
    return count


async def notify_new_thread(
    storage: Storage,
    thread_id: int,
    session_id: int,
    actor_id: int,
) -> int:
    """Notify relevant users about a new thread."""
    users = await storage.get_users_for_notification(session_id, "new_thread")
    count = 0
    for u in users:
        if u["id"] == actor_id:
            continue
        await storage.create_notification(
            u["id"],
            "new_thread",
            source_thread_id=thread_id,
            session_id=session_id,
            actor_id=actor_id,
            message="started a new discussion",
        )
        count += 1
    return count


async def notify_reply(
    storage: Storage,
    comment_id: int,
    thread_id: int,
    session_id: int,
    actor_id: int,
) -> int:
    """Notify relevant users about a reply in a thread."""
    users = await storage.get_users_for_notification(session_id, "reply")
    count = 0
    for u in users:
        if u["id"] == actor_id:
            continue
        await storage.create_notification(
            u["id"],
            "reply",
            source_thread_id=thread_id,
            source_comment_id=comment_id,
            session_id=session_id,
            actor_id=actor_id,
            message="replied in a discussion",
        )
        count += 1
    return count


async def notify_resolved(
    storage: Storage,
    thread_id: int,
    session_id: int,
    actor_id: int,
) -> int:
    """Notify relevant users that a thread was resolved."""
    users = await storage.get_users_for_notification(session_id, "resolved")
    count = 0
    for u in users:
        if u["id"] == actor_id:
            continue
        await storage.create_notification(
            u["id"],
            "resolved",
            source_thread_id=thread_id,
            session_id=session_id,
            actor_id=actor_id,
            message="resolved a discussion",
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Pluggable channel interface
# ---------------------------------------------------------------------------


class NotificationChannel(ABC):
    """Abstract notification delivery channel."""

    @abstractmethod
    async def send(self, user_id: int, notification: dict[str, Any]) -> bool:
        """Deliver a notification. Returns True on success."""


class PlatformChannel(NotificationChannel):
    """In-app notification — always on, writes to notifications table."""

    async def send(self, user_id: int, notification: dict[str, Any]) -> bool:
        # Platform notifications are created directly via storage.create_notification
        # This channel exists for interface completeness.
        return True


class EmailChannel(NotificationChannel):
    """Email notification channel — respects user frequency preferences."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    async def send(self, user_id: int, notification: dict[str, Any]) -> bool:
        from helmlog.email import send_email, smtp_configured

        if not smtp_configured():
            return False

        user = await self._storage.get_user_by_id(user_id)
        if not user or not user.get("email"):
            return False

        subject = "HelmLog — New notification"
        actor_name = notification.get("actor_name", "Someone")
        message = notification.get("message", "")
        body = (
            f"Hi {user.get('name', 'there')},\n\n"
            f"{actor_name} {message}.\n\n"
            f"Log in to HelmLog to view the full discussion.\n\n"
            f"Fair winds!"
        )
        return await send_email(user["email"], subject, body)


# ---------------------------------------------------------------------------
# Email dispatch
# ---------------------------------------------------------------------------


async def dispatch_email_notifications(storage: Storage) -> int:
    """Send pending email notifications based on user preferences.

    Returns the number of emails sent.
    """
    from helmlog.email import smtp_configured

    if not smtp_configured():
        return 0

    channel = EmailChannel(storage)
    sent = 0

    # Get all users who have email notifications enabled (immediate)
    db = storage._conn()
    cur = await db.execute(
        "SELECT DISTINCT n.id, n.user_id, n.type, n.message, n.actor_id,"
        " u.name AS actor_name"
        " FROM notifications n"
        " LEFT JOIN users u ON n.actor_id = u.id"
        " WHERE n.read = 0 AND n.dismissed = 0"
        " AND EXISTS ("
        "   SELECT 1 FROM notification_preferences np"
        "   WHERE np.user_id = n.user_id AND np.channel = 'email'"
        "   AND np.type = n.type AND np.enabled = 1"
        "   AND np.frequency = 'immediate'"
        " )"
        " ORDER BY n.created_at"
    )
    rows = await cur.fetchall()

    for row in rows:
        notif = dict(row)
        try:
            ok = await channel.send(notif["user_id"], notif)
            if ok:
                sent += 1
        except Exception:  # noqa: BLE001
            logger.warning("Failed to send email notification {}", notif["id"])

    return sent
