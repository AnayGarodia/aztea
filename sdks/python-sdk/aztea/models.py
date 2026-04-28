from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import results as _results


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

    def __rich__(self) -> object:
        return _results.record_table(
            f"Agent {self.name}",
            [
                ("ID", self.agent_id),
                ("Price", f"${self.price_per_call_usd:.2f}"),
                ("Trust", f"{self.trust_score:.0f}/100"),
                ("Success", f"{self.success_rate:.0%}"),
                ("Calls", self.total_calls),
                ("Tags", self.tags),
                ("Description", self.description),
            ],
        )


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
    _client: Any | None = field(default=None, repr=False, compare=False)

    def bind_client(self, client: Any) -> "Job":
        self._client = client
        return self

    def full(self) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("This job is not bound to an AzteaClient; full output is unavailable.")
        return self._client.get_job_full_output(self.job_id)

    def __rich__(self) -> object:
        rows = [
            ("Job", self.job_id),
            ("Agent", self.agent_id),
            ("Status", self.status),
            ("Cost", f"${self.price_cents / 100:.2f}"),
            ("Created", self.created_at),
            ("Completed", self.completed_at or "-"),
        ]
        payload = self.output_payload if self.output_payload is not None else {"error": self.error_message}
        return _results.stack_renderables(
            _results.record_table(f"Job {self.job_id[:8]}", rows),
            _results.job_payload_panel("Output", payload),
        )


@dataclass(slots=True)
class JobResult:
    job_id: str
    output: dict[str, Any]
    cost_cents: int
    quality_score: float | None = None
    error: str | None = None
    _client: Any | None = field(default=None, repr=False, compare=False)

    def bind_client(self, client: Any) -> "JobResult":
        self._client = client
        return self

    def full(self) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("This result is not bound to an AzteaClient; full output is unavailable.")
        return self._client.get_job_full_output(self.job_id)

    def __rich__(self) -> object:
        header = _results.record_table(
            f"Result {self.job_id[:8]}",
            [
                ("Job", self.job_id),
                ("Cost", f"${self.cost_cents / 100:.2f}"),
                ("Quality", self.quality_score if self.quality_score is not None else "-"),
                ("Error", self.error or "-"),
            ],
        )
        return _results.stack_renderables(header, _results.job_payload_panel("Output", self.output))


@dataclass(slots=True)
class Transaction:
    tx_id: str
    wallet_id: str
    type: str
    amount_cents: int
    memo: str = ""
    agent_id: str | None = None
    created_at: str = ""

    def __rich__(self) -> object:
        return _results.record_table(
            f"Transaction {self.tx_id[:8] if self.tx_id else 'new'}",
            [
                ("Wallet", self.wallet_id),
                ("Type", self.type),
                ("Amount", f"${self.amount_cents / 100:.2f}"),
                ("Memo", self.memo),
                ("Agent", self.agent_id or "-"),
                ("Created", self.created_at or "-"),
            ],
        )


@dataclass(slots=True)
class Wallet:
    wallet_id: str
    owner_id: str
    balance_cents: int
    caller_trust: float = 0.5
    created_at: str = ""

    def __rich__(self) -> object:
        return _results.record_table(
            "Wallet",
            [
                ("Wallet", self.wallet_id),
                ("Owner", self.owner_id),
                ("Balance", f"${self.balance_cents / 100:.2f}"),
                ("Trust", f"{self.caller_trust:.0%}"),
                ("Created", self.created_at or "-"),
            ],
        )


@dataclass(slots=True)
class VerificationContract:
    required_keys: list[str] = field(default_factory=list)
    field_types: dict[str, str] = field(default_factory=dict)
    field_ranges: dict[str, dict[str, float]] = field(default_factory=dict)
