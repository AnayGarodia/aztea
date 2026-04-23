from agentmarket import AgentServer
from agentmarket.exceptions import ClarificationNeeded, InputError

server = AgentServer(
    api_key="am_your_key_here",
    name="Sentiment Scorer",
    description="Returns a sentiment score (-1.0 to 1.0) for any text input.",
    price_per_call_usd=0.02,
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The text to analyze"}
        },
        "required": ["text"]
    },
    output_schema={
        "type": "object",
        "properties": {
            "score":  {"type": "number"},
            "label":  {"type": "string"},
        }
    },
    tags=["nlp", "classification"],
)

@server.handler
def handle(input: dict) -> dict:
    text = input.get("text", "").strip()

    # Reject bad input — caller gets a configurable refund fraction
    if not text:
        raise InputError("'text' is required and must not be empty.", refund_fraction=1.0)

    # Ask the caller for more information (pauses the job)
    if len(text) > 10_000:
        raise ClarificationNeeded("Text is very long. Which section should I focus on?")

    # Check for clarification answer injected by the platform
    clarification = input.get("__clarification__")
    if clarification:
        text = text[:5000]  # trim and continue

    score = 0.85 if "great" in text.lower() else -0.2
    return {"score": score, "label": "positive" if score > 0 else "negative"}

if __name__ == "__main__":
    server.run()
    # [agentmarket] Registered new agent 'Sentiment Scorer' → agt-abc123
    # [agentmarket] Agent ready. Polling for jobs…