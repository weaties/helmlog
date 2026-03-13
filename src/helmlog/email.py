"""Optional SMTP email support (#94).

All sends are best-effort: log a warning on failure, never crash the caller.
Email is only sent when all required SMTP_* env vars are configured.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
from email.message import EmailMessage

from loguru import logger


def smtp_configured() -> bool:
    """Return True if all required SMTP env vars are set."""
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM"))


def _build_message(to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def _send_sync(msg: EmailMessage) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")

    with smtplib.SMTP(host, port, timeout=10) as srv:
        srv.ehlo()
        srv.starttls()
        srv.ehlo()
        if user and password:
            srv.login(user, password)
        srv.send_message(msg)


async def send_email(to: str, subject: str, body: str) -> bool:
    """Send an email via SMTP in a background thread. Returns True on success."""
    try:
        msg = _build_message(to, subject, body)
        await asyncio.to_thread(_send_sync, msg)
        logger.info("Email sent to {} — {}", to, subject)
        return True
    except Exception as exc:
        logger.warning("Failed to send email to {}: {}", to, exc)
        return False


async def send_welcome_email(name: str | None, email: str, role: str, login_url: str) -> bool:
    """Send a welcome/invite email with the login link."""
    greeting = f"Hi {name}" if name else "Hi"
    subject = "You're invited to HelmLog"
    body = (
        f"{greeting},\n\n"
        f"You've been added as a {role} on HelmLog.\n\n"
        f"Click the link below to log in (expires in 7 days):\n"
        f"  {login_url}\n\n"
        f"Fair winds!"
    )
    return await send_email(email, subject, body)


async def send_login_link_email(name: str | None, email: str, login_url: str) -> bool:
    """Send a self-service login link email."""
    greeting = f"Hi {name}" if name else "Hi"
    subject = "Your login link — HelmLog"
    body = (
        f"{greeting},\n\n"
        f"You requested a login link for HelmLog.\n\n"
        f"Click the link below to sign in (expires in 7 days):\n"
        f"  {login_url}\n\n"
        f"If you didn't request this, you can safely ignore this email.\n\n"
        f"Fair winds!"
    )
    return await send_email(email, subject, body)


async def send_password_reset_email(name: str | None, email: str, reset_url: str) -> bool:
    """Send a password reset email with the reset link."""
    greeting = f"Hi {name}" if name else "Hi"
    subject = "Reset your password — HelmLog"
    body = (
        f"{greeting},\n\n"
        f"You requested a password reset for HelmLog.\n\n"
        f"Click the link below to reset your password (expires in 1 hour):\n"
        f"  {reset_url}\n\n"
        f"If you didn't request this, you can safely ignore this email.\n\n"
        f"Fair winds!"
    )
    return await send_email(email, subject, body)


async def send_device_alert(user_email: str, ip: str | None, user_agent: str | None) -> bool:
    """Notify a user about a new device login."""
    subject = "New device login — HelmLog"
    body = (
        f"A new session was created for your account.\n\n"
        f"IP: {ip or 'unknown'}\n"
        f"Device: {user_agent or 'unknown'}\n\n"
        f"If this wasn't you, contact your admin."
    )
    return await send_email(user_email, subject, body)
