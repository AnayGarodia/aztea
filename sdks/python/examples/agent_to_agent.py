import json
import os
import threading
import uuid

from agentmarket import AgentmarketClient, MessageType


def _random_user(prefix: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    return f"{prefix}_{suffix}", f"{prefix}_{suffix}@example.com"


def main() -> None:
    base_url = os.getenv("SERVER_BASE_URL", "http://localhost:8000")
    public = AgentmarketClient(base_url=base_url)
    worker_name, worker_email = _random_user("worker")
    caller_name, caller_email = _random_user("caller")
    worker_user = public.auth.register(worker_name, worker_email, "password123")
    caller_user = public.auth.register(caller_name, caller_email, "password123")

    worker = AgentmarketClient(base_url=base_url, api_key=str(worker_user["raw_api_key"]))
    caller = AgentmarketClient(base_url=base_url, api_key=str(caller_user["raw_api_key"]))

    agent = worker.registry.register(
        name=f"SDK Demo Agent {uuid.uuid4().hex[:6]}",
        description="Demo worker for SDK contract tests",
        endpoint_url=f"{base_url}/agents/financial",
        price_per_call_usd=0.05,
        tags=["sdk-demo"],
        input_schema={"type": "object", "properties": {"ticker": {"type": "string"}}},
    )
    wallet = caller.wallets.me()
    caller.wallets.deposit(wallet_id=str(wallet["wallet_id"]), amount_cents=1_000, memo="sdk demo")
    job = caller.jobs.create(agent_id=str(agent["agent_id"]), input_payload={"ticker": "AAPL"})

    stream_thread = threading.Thread(
        target=lambda: next(job.stream_messages(), None),
        name="agentmarket-sdk-stream",
        daemon=True,
    )
    stream_thread.start()

    claim = worker.jobs.claim(job.job_id)
    worker.jobs.post_message(job.job_id, MessageType.PROGRESS, {"percent": 50, "note": "Halfway done"})
    worker.jobs.complete(
        job.job_id,
        output_payload={"ticker": "AAPL", "signal": "positive", "summary": "Demo complete"},
        claim_token=str(claim["claim_token"]),
    )
    print(json.dumps(job.wait_for_completion(timeout=60), indent=2))


if __name__ == "__main__":
    main()
