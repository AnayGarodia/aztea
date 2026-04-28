"""
email.py — Transactional email helpers. Gracefully disabled if SMTP is not configured.

Configure via environment variables:
  SMTP_HOST       e.g. smtp.sendgrid.net  (or smtp.resend.com)
  SMTP_PORT       e.g. 587
  SMTP_USER       e.g. apikey
  SMTP_PASSWORD   e.g. your SMTP password or API key
  FROM_EMAIL      e.g. noreply@aztea.ai
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
_FROM_EMAIL    = os.environ.get("FROM_EMAIL", "noreply@aztea.ai").strip()
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


def send_welcome(to: str, username: str, role: str = "both") -> None:
    """Send a welcome email to a newly registered user. No-ops if SMTP_HOST is unset."""
    safe_username = _esc(username)
    if role == "builder":
        html_body = (
            f"<p>Hi {safe_username},</p>"
            "<p>Welcome to Aztea! You're signed up as a <strong>builder</strong>.</p>"
            "<p>Upload your first <code>SKILL.md</code>, set a price, and start earning — "
            "you keep <strong>90%</strong> of every successful call.</p>"
            "<p><a href='https://aztea.ai/list-skill'>List your first skill →</a></p>"
            "<p>— The Aztea team</p>"
        )
        text_body = (
            f"Hi {username},\n\nWelcome to Aztea! You're signed up as a builder.\n\n"
            "Upload your first SKILL.md, set a price, and start earning — you keep 90% of every call.\n\n"
            "Get started: https://aztea.ai/list-skill\n\n— The Aztea team"
        )
    elif role == "hirer":
        html_body = (
            f"<p>Hi {safe_username},</p>"
            "<p>Welcome to Aztea! We've added <strong>$2.00</strong> of free credit to your wallet — "
            "no card needed.</p>"
            "<p>Browse agents, run a job, and see results immediately.</p>"
            "<p><a href='https://aztea.ai/agents'>Browse agents →</a></p>"
            "<p>— The Aztea team</p>"
        )
        text_body = (
            f"Hi {username},\n\nWelcome to Aztea! We've added $2.00 of free credit to your wallet — no card needed.\n\n"
            "Browse agents and run your first job: https://aztea.ai/agents\n\n— The Aztea team"
        )
    else:
        html_body = (
            f"<p>Hi {safe_username},</p>"
            "<p>Welcome to Aztea! We've added <strong>$1.00</strong> of credit to your wallet to get started.</p>"
            "<p>Browse agents to hire, or upload a <code>SKILL.md</code> to start earning.</p>"
            "<p>— The Aztea team</p>"
        )
        text_body = (
            f"Hi {username},\n\nWelcome to Aztea! We've added $1.00 of credit to get you started.\n\n"
            "— The Aztea team"
        )
    send(to, "Welcome to Aztea", html_body, text_body)


def send_skill_live(
    to: str,
    username: str,
    skill_name: str,
    price_usd: float,
    endpoint_url: str,
) -> None:
    """Notify the agent owner that their skill is live on the marketplace. No-ops if SMTP_HOST is unset."""
    safe_name = _esc(skill_name)
    safe_endpoint = _esc(endpoint_url)
    safe_username = _esc(username)
    price_fmt = f"${price_usd:.2f}"
    payout_fmt = f"${price_usd * 0.9:.3f}"
    send(
        to,
        f"Your skill is live — {skill_name}",
        f"<p>Hi {safe_username},</p>"
        f"<p>Your skill <strong>{safe_name}</strong> is now live on the Aztea marketplace.</p>"
        f"<table style='border-collapse:collapse;margin:16px 0'>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>Price per call</td>"
        f"<td style='padding:4px 0'><strong>{price_fmt}</strong></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>Your cut (90%)</td>"
        f"<td style='padding:4px 0;color:#16a34a'><strong>{payout_fmt}</strong></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>Endpoint</td>"
        f"<td style='padding:4px 0'><code style='font-size:0.85em'>{safe_endpoint}</code></td></tr>"
        f"</table>"
        f"<p>Callers can discover and hire your skill now. Payouts land in your wallet after each successful job.</p>"
        f"<p><a href='https://aztea.ai/worker'>Open your worker dashboard →</a></p>"
        "<p>— The Aztea team</p>",
        f"Hi {username},\n\nYour skill '{skill_name}' is now live on the Aztea marketplace.\n\n"
        f"Price per call: {price_fmt}\nYour cut (90%): {payout_fmt}\nEndpoint: {endpoint_url}\n\n"
        "Callers can hire it now. Payouts land in your wallet after each successful job.\n\n"
        "Worker dashboard: https://aztea.ai/worker\n\n— The Aztea team",
    )


def send_payout_received(
    to: str,
    username: str,
    payout_cents: int,
    job_id: str,
    skill_name: str,
) -> None:
    """Email the agent owner their payout amount after a successful job. No-ops if SMTP_HOST is unset."""
    safe_name = _esc(skill_name)
    safe_job_id = _esc(job_id)
    safe_username = _esc(username)
    payout_fmt = f"${payout_cents / 100:.2f}"
    send(
        to,
        f"Payout received — {payout_fmt} for {skill_name}",
        f"<p>Hi {safe_username},</p>"
        f"<p><strong>{payout_fmt}</strong> has been credited to your wallet for a completed job on "
        f"<strong>{safe_name}</strong>.</p>"
        f"<p style='color:#666;font-size:0.9em'>Job ID: <code>{safe_job_id}</code></p>"
        f"<p><a href='https://aztea.ai/wallet'>View your wallet →</a></p>"
        "<p>— The Aztea team</p>",
        f"Hi {username},\n\n{payout_fmt} credited to your wallet for a completed job on '{skill_name}'.\n"
        f"Job ID: {job_id}\n\nWallet: https://aztea.ai/wallet\n\n— The Aztea team",
    )


def send_job_complete(to: str, job_id: str, agent_name: str, price_cents: int) -> None:
    """Email the caller that their job finished successfully. No-ops if SMTP_HOST is unset."""
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
    """Email the caller that their job failed and they've been refunded. No-ops if SMTP_HOST is unset."""
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
    """Notify both parties that a dispute has been filed. No-ops if SMTP_HOST is unset."""
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
    """Notify the relevant party of the dispute outcome. No-ops if SMTP_HOST is unset."""
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


def send_signup_verification_otp(to: str, otp: str) -> None:
    """Send a 6-digit OTP for new account email verification (expires in 15 min). No-ops if SMTP_HOST is unset."""
    safe_otp = _esc(otp)
    send(
        to,
        "Your Aztea verification code",
        f"<p>Welcome to Aztea! Use this code to finish creating your account:</p>"
        f"<p style='font-size:2rem;font-weight:700;letter-spacing:0.15em;font-family:monospace'>{safe_otp}</p>"
        f"<p>This code expires in 15 minutes. If you didn't try to sign up, you can safely ignore this email.</p>",
        f"Your Aztea verification code: {otp}\n\nExpires in 15 minutes. Ignore if you didn't try to sign up.",
    )


def send_password_reset_otp(to: str, otp: str) -> None:
    """Send a password-reset OTP (expires in 15 min). No-ops if SMTP_HOST is unset."""
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
    """Send a confirmation email when a withdrawal has been submitted to Stripe. No-ops if SMTP_HOST is unset."""
    amount_fmt = f"${amount_cents / 100:.2f}"
    send(
        to,
        "Withdrawal processed",
        f"<p>Your withdrawal of <strong>{amount_fmt}</strong> has been submitted to your connected Stripe account.</p>"
        "<p>Funds typically arrive within 1–2 business days.</p>",
        f"Your withdrawal of {amount_fmt} has been submitted. Funds typically arrive within 1–2 business days.",
    )
