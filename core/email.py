"""
email.py — Transactional email helpers. Gracefully disabled if SMTP is not configured.

Configure via environment variables:
  SMTP_HOST       e.g. smtp.sendgrid.net  (or smtp.resend.com)
  SMTP_PORT       e.g. 587
  SMTP_USER       e.g. apikey
  SMTP_PASSWORD   e.g. your SMTP password or API key
  FROM_EMAIL      e.g. noreply@aztea.dev
  FROM_NAME       e.g. Aztea
"""
from __future__ import annotations

import html
import logging
import os
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

_LOG = logging.getLogger(__name__)

_SMTP_HOST     = os.environ.get("SMTP_HOST", "").strip()
_SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587") or "587")
_SMTP_USER     = os.environ.get("SMTP_USER", "").strip()
_SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
_FROM_EMAIL    = os.environ.get("FROM_EMAIL", "noreply@aztea.dev").strip()
_FROM_NAME     = os.environ.get("FROM_NAME", "Aztea").strip()

ENABLED = bool(_SMTP_HOST and _SMTP_USER and _SMTP_PASSWORD)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _send_sync(to: str, subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{_FROM_NAME} <{_FROM_EMAIL}>"
    msg["To"] = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(_SMTP_USER, _SMTP_PASSWORD)
            server.sendmail(_FROM_EMAIL, [to], msg.as_string())
    except Exception:
        _LOG.exception("Email send failed: to=%s subject=%s", to, subject)


def send(to: str, subject: str, html: str, text: str) -> None:
    """Send an email in a background thread. No-op if SMTP is not configured."""
    if not ENABLED or not to:
        return
    threading.Thread(target=_send_sync, args=(to, subject, html, text), daemon=True).start()


def send_welcome(to: str, username: str) -> None:
    safe_username = _esc(username)
    send(
        to,
        "Welcome to Aztea",
        f"<p>Hi {safe_username},</p>"
        "<p>Welcome to Aztea! We've added <strong>$1.00</strong> of credit to your wallet to get started.</p>"
        "<p>— The Aztea team</p>",
        f"Hi {username},\n\nWelcome to Aztea! We've added $1.00 of credit to get you started.\n\n— The Aztea team",
    )


def send_job_complete(to: str, job_id: str, agent_name: str, price_cents: int) -> None:
    price_fmt = f"${price_cents / 100:.2f}"
    safe_agent_name = _esc(agent_name)
    safe_job_id = _esc(job_id)
    send(
        to,
        f"Job complete — {agent_name}",
        f"<p>Your job on <strong>{safe_agent_name}</strong> has completed.</p>"
        f"<p>Job ID: <code>{safe_job_id}</code> &nbsp;·&nbsp; Charged: <strong>{price_fmt}</strong></p>",
        f"Your job on {agent_name} has completed.\nJob ID: {job_id}\nCharged: {price_fmt}",
    )


def send_job_failed(to: str, job_id: str, agent_name: str, error: str) -> None:
    safe_agent_name = _esc(agent_name)
    safe_job_id = _esc(job_id)
    safe_error = _esc(error)
    send(
        to,
        f"Job failed — {agent_name}",
        f"<p>Your job on <strong>{safe_agent_name}</strong> failed. You have been fully refunded.</p>"
        f"<p>Job ID: <code>{safe_job_id}</code> &nbsp;·&nbsp; Reason: {safe_error}</p>",
        f"Your job on {agent_name} failed and you've been refunded.\nJob ID: {job_id}\nReason: {error}",
    )


def send_deposit_confirmed(to: str, amount_cents: int) -> None:
    amount_fmt = f"${amount_cents / 100:.2f}"
    send(
        to,
        "Deposit confirmed",
        f"<p>Your deposit of <strong>{amount_fmt}</strong> has been confirmed and added to your wallet.</p>",
        f"Your deposit of {amount_fmt} has been confirmed and added to your wallet.",
    )


def send_dispute_opened(to: str, job_id: str, dispute_id: str) -> None:
    safe_job_id = _esc(job_id)
    safe_dispute_id = _esc(dispute_id)
    send(
        to,
        "Dispute filed",
        f"<p>A dispute has been filed for job <code>{safe_job_id}</code> (ID: <code>{safe_dispute_id}</code>).</p>"
        "<p>Our judges will review the case and notify you of the outcome.</p>",
        f"A dispute has been filed for job {job_id} (dispute {dispute_id}). Our judges will review shortly.",
    )


def send_dispute_resolved(to: str, job_id: str, dispute_id: str, outcome: str) -> None:
    safe_job_id = _esc(job_id)
    safe_dispute_id = _esc(dispute_id)
    safe_outcome = _esc(outcome)
    send(
        to,
        "Dispute resolved",
        f"<p>The dispute for job <code>{safe_job_id}</code> (ID: <code>{safe_dispute_id}</code>) has been resolved.</p>"
        f"<p>Outcome: <strong>{safe_outcome}</strong></p>",
        f"Dispute {dispute_id} for job {job_id} resolved. Outcome: {outcome}",
    )


def send_password_reset_otp(to: str, otp: str) -> None:
    safe_otp = _esc(otp)
    send(
        to,
        "Your Aztea password reset code",
        f"<p>Your one-time password reset code is:</p>"
        f"<p style='font-size:2rem;font-weight:700;letter-spacing:0.15em;font-family:monospace'>{safe_otp}</p>"
        f"<p>This code expires in 15 minutes. If you didn't request a reset, you can safely ignore this email.</p>",
        f"Your Aztea password reset code: {otp}\n\nExpires in 15 minutes. Ignore if you didn't request this.",
    )


def send_withdrawal_processed(to: str, amount_cents: int) -> None:
    amount_fmt = f"${amount_cents / 100:.2f}"
    send(
        to,
        "Withdrawal processed",
        f"<p>Your withdrawal of <strong>{amount_fmt}</strong> has been submitted to your connected Stripe account.</p>"
        "<p>Funds typically arrive within 1–2 business days.</p>",
        f"Your withdrawal of {amount_fmt} has been submitted. Funds typically arrive within 1–2 business days.",
    )
