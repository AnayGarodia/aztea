"""
email.py — Transactional email helpers. Gracefully disabled if SMTP is not configured.

Configure via environment variables:
  SMTP_HOST       e.g. smtp.sendgrid.net  (or smtp.resend.com)
  SMTP_PORT       e.g. 587
  SMTP_USER       e.g. apikey
  SMTP_PASSWORD   e.g. your SMTP password or API key
  FROM_EMAIL      e.g. noreply@agentmarket.dev
  FROM_NAME       e.g. AgentMarket
"""
from __future__ import annotations

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
_FROM_EMAIL    = os.environ.get("FROM_EMAIL", "noreply@agentmarket.dev").strip()
_FROM_NAME     = os.environ.get("FROM_NAME", "AgentMarket").strip()

ENABLED = bool(_SMTP_HOST and _SMTP_USER and _SMTP_PASSWORD)


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
    send(
        to,
        "Welcome to AgentMarket",
        f"<p>Hi {username},</p>"
        "<p>Welcome to AgentMarket! We've added <strong>$1.00</strong> of credit to your wallet to get started.</p>"
        "<p>— The AgentMarket team</p>",
        f"Hi {username},\n\nWelcome to AgentMarket! We've added $1.00 of credit to get you started.\n\n— The AgentMarket team",
    )


def send_job_complete(to: str, job_id: str, agent_name: str, price_cents: int) -> None:
    price_fmt = f"${price_cents / 100:.2f}"
    send(
        to,
        f"Job complete — {agent_name}",
        f"<p>Your job on <strong>{agent_name}</strong> has completed.</p>"
        f"<p>Job ID: <code>{job_id}</code> &nbsp;·&nbsp; Charged: <strong>{price_fmt}</strong></p>",
        f"Your job on {agent_name} has completed.\nJob ID: {job_id}\nCharged: {price_fmt}",
    )


def send_job_failed(to: str, job_id: str, agent_name: str, error: str) -> None:
    send(
        to,
        f"Job failed — {agent_name}",
        f"<p>Your job on <strong>{agent_name}</strong> failed. You have been fully refunded.</p>"
        f"<p>Job ID: <code>{job_id}</code> &nbsp;·&nbsp; Reason: {error}</p>",
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
    send(
        to,
        "Dispute filed",
        f"<p>A dispute has been filed for job <code>{job_id}</code> (ID: <code>{dispute_id}</code>).</p>"
        "<p>Our judges will review the case and notify you of the outcome.</p>",
        f"A dispute has been filed for job {job_id} (dispute {dispute_id}). Our judges will review shortly.",
    )


def send_dispute_resolved(to: str, job_id: str, dispute_id: str, outcome: str) -> None:
    send(
        to,
        "Dispute resolved",
        f"<p>The dispute for job <code>{job_id}</code> (ID: <code>{dispute_id}</code>) has been resolved.</p>"
        f"<p>Outcome: <strong>{outcome}</strong></p>",
        f"Dispute {dispute_id} for job {job_id} resolved. Outcome: {outcome}",
    )
