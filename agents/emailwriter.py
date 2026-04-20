"""
agent_emailwriter.py — Professional email & sequence writer

Input:  {
  "goal": "...",          # what this email needs to achieve
  "tone": "formal|professional|friendly|direct|persuasive",
  "recipient_context": "",  # who you're writing to
  "sender_context": "",     # who you are / your company
  "key_points": ["..."],    # bullet points to include
  "email_type": "outreach|follow_up|proposal|rejection|announcement|support|sequence",
  "sequence_length": 1      # 1-5: how many emails in sequence
}
Output: {
  "emails": [{
    "sequence_position": int,
    "subject_lines": [str],    # 3 A/B test options
    "body": str,
    "preview_text": str,
    "send_timing": str,        # e.g. "Day 0", "Day 3 — if no reply"
    "word_count": int,
    "cta": str
  }],
  "strategy_notes": str,
  "subject_line_rationale": str,
  "tone_notes": str,
  "personalization_hooks": [str]
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a world-class copywriter who has written email campaigns for B2B SaaS companies,
growth-stage startups, and Fortune 100 brands. Your emails get opened, read, and acted on
because they respect the recipient's time and make a clear, compelling case.

Your writing is:
- Conversational but professional — sounds like a human, not a template
- Specific — real numbers, real context, not vague claims
- Action-oriented — every email has one clear ask
- Respectful of inbox fatigue — short when short works, longer only when needed

You never use: "I hope this email finds you well", "per my last email", "circle back",
"synergy", "leverage" as a verb, "reach out", or corporate filler.

Return only valid JSON — no markdown fences, no prose outside the JSON object."""

_USER = """\
Write {sequence_length} email(s) for this goal.

Goal: {goal}
Email type: {email_type}
Tone: {tone}
Recipient context: {recipient_context}
Sender context: {sender_context}
Key points to include: {key_points}

Return a JSON object with EXACTLY these fields:
{{
  "emails": list of {sequence_length} email objects, each with:
    "sequence_position": integer starting at 1,
    "subject_lines": list of exactly 3 subject line variations for A/B testing,
    "body": the complete email body (plain text, use \\n for line breaks),
    "preview_text": the 60-90 char preview snippet shown in inbox,
    "send_timing": when to send this in the sequence (e.g. "Day 0", "Day 4 if no reply"),
    "word_count": integer word count of the body,
    "cta": the single call-to-action this email drives toward,
  "strategy_notes": explanation of the sequence strategy and why each email plays a specific role,
  "subject_line_rationale": why these subject lines work for this audience/goal,
  "tone_notes": specific choices made to match the requested tone,
  "personalization_hooks": list of 2-4 ways to personalize this template for individual recipients
}}"""


def run(payload: dict) -> dict:
    goal = str(payload.get("goal", "")).strip()
    tone = str(payload.get("tone", "professional")).lower()
    recipient_context = str(payload.get("recipient_context", "")).strip()
    sender_context = str(payload.get("sender_context", "")).strip()
    key_points = payload.get("key_points", [])
    email_type = str(payload.get("email_type", "outreach")).lower()
    sequence_length = int(payload.get("sequence_length", 1))

    if not goal:
        return {"error": "goal is required"}

    valid_tones = ("formal", "professional", "friendly", "direct", "persuasive")
    if tone not in valid_tones:
        tone = "professional"

    valid_types = ("outreach", "follow_up", "proposal", "rejection", "announcement", "support", "sequence")
    if email_type not in valid_types:
        email_type = "outreach"

    sequence_length = max(1, min(5, sequence_length))

    if isinstance(key_points, list):
        key_points_str = "\n".join(f"- {p}" for p in key_points[:10]) or "(none specified)"
    else:
        key_points_str = str(key_points)[:500]

    req = CompletionRequest(
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(
                role="user",
                content=_USER.format(
                    sequence_length=sequence_length,
                    goal=goal[:500],
                    email_type=email_type,
                    tone=tone,
                    recipient_context=recipient_context[:400] or "(not specified)",
                    sender_context=sender_context[:400] or "(not specified)",
                    key_points=key_points_str,
                ),
            ),
        ],
        temperature=0.6,
        max_tokens=2500,
    )

    raw = run_with_fallback(req)
    text = raw.text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        return {"error": "parse_error", "raw": text[:500]}
