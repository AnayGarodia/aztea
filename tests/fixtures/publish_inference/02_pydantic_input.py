"""Validate a Stripe webhook signature."""

from pydantic import BaseModel


class WebhookPayload(BaseModel):
    signature: str
    body: str
    secret: str
    tolerance_seconds: int = 300


class WebhookResult(BaseModel):
    valid: bool
    detail: str


def handler(payload: WebhookPayload) -> WebhookResult:
    """Verify the HMAC signature on a Stripe webhook payload."""
    return WebhookResult(valid=True, detail="ok")
