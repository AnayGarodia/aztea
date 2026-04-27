from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Agent:
    agent_id: str
    name: str
    description: str
    endpoint_url: str
    price_per_call_usd: float
    tags: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
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
        return round(float(self.price_per_call_usd) * 100)


@dataclass(slots=True)
class Job:
    job_id: str
    agent_id: str
    status: str
    price_cents: int = 0
    input_payload: dict[str, Any] = field(default_factory=dict)
    output_payload: dict[str, Any] | None = None
    error_message: str | None = None
    quality_score: int | None = None
    claim_token: str | None = None
    parent_job_id: str | None = None
    parent_cascade_policy: str | None = None
    clarification_timeout_seconds: int | None = None
    clarification_timeout_policy: str | None = None
    clarification_requested_at: str | None = None
    clarification_deadline_at: str | None = None
    output_verification_window_seconds: int | None = None
    output_verification_status: str | None = None
    output_verification_deadline_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None


@dataclass(slots=True)
class JobResult:
    job_id: str
    output: dict[str, Any]
    cost_cents: int
    quality_score: float | None = None
    error: str | None = None


@dataclass(slots=True)
class Transaction:
    tx_id: str
    wallet_id: str
    type: str
    amount_cents: int
    memo: str = ""
    agent_id: str | None = None
    created_at: str = ""


@dataclass(slots=True)
class Wallet:
    wallet_id: str
    owner_id: str
    balance_cents: int
    caller_trust: float = 0.5
    created_at: str = ""


@dataclass(slots=True)
class VerificationContract:
    required_keys: list[str] = field(default_factory=list)
    field_types: dict[str, str] = field(default_factory=dict)
    field_ranges: dict[str, dict[str, float]] = field(default_factory=dict)
