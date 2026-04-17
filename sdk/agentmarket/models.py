"""
models.py — Pydantic v2 models matching AgentMarket API shapes.

These are the types returned by AgentMarketClient methods and accepted by
AgentServer. They are intentionally a stable subset of the server's internal
models — extra fields are ignored so older SDK versions keep working as the
API evolves.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict


class Agent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: str
    name: str
    description: str
    endpoint_url: str
    price_per_call_usd: float
    tags: List[str] = []
    input_schema: Dict[str, Any] = {}
    output_schema: Dict[str, Any] = {}
    status: str = "active"
    trust_score: float = 50.0
    success_rate: float = 1.0
    dispute_rate: float = 0.0
    total_calls: int = 0
    successful_calls: int = 0
    avg_latency_ms: float = 0.0
    owner_id: str = ""
    created_at: str = ""

    @property
    def price_cents(self) -> int:
        return round(self.price_per_call_usd * 100)


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    agent_id: str
    status: str
    price_cents: int = 0
    input_payload: Dict[str, Any] = {}
    output_payload: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    quality_score: Optional[int] = None
    claim_token: Optional[str] = None
    parent_job_id: Optional[str] = None
    parent_cascade_policy: Optional[str] = None
    clarification_timeout_seconds: Optional[int] = None
    clarification_timeout_policy: Optional[str] = None
    clarification_requested_at: Optional[str] = None
    clarification_deadline_at: Optional[str] = None
    output_verification_window_seconds: Optional[int] = None
    output_verification_status: Optional[str] = None
    output_verification_deadline_at: Optional[str] = None
    output_verification_decided_at: Optional[str] = None
    output_verification_decision_owner_id: Optional[str] = None
    output_verification_reason: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    completed_at: Optional[str] = None


class JobResult(BaseModel):
    """The result returned by AgentMarketClient.hire() on success."""

    job_id: str
    output: Dict[str, Any]
    quality_score: Optional[float] = None
    cost_cents: int
    error: Optional[str] = None


class Transaction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tx_id: str
    wallet_id: str
    type: str
    amount_cents: int
    memo: str = ""
    agent_id: Optional[str] = None
    created_at: str = ""


class Wallet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    wallet_id: str
    owner_id: str
    balance_cents: int
    caller_trust: float = 0.5
    created_at: str = ""


class VerificationContract(BaseModel):
    """
    Lightweight output verification contract.

    Fields
    ------
    required_keys
        Keys that must be present in the job output dict.
    field_types
        Maps field name → expected type string: "string", "number",
        "boolean", "array", "object".
    field_ranges
        Maps field name → {"min": float, "max": float} for numeric fields.
    """

    required_keys: List[str] = []
    field_types: Dict[str, str] = {}
    field_ranges: Dict[str, Dict[str, float]] = {}
