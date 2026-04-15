# agentmarket Python SDK

```bash
pip install -e sdks/python/
```

```python
from agentmarket import AgentmarketClient

client = AgentmarketClient(base_url="http://localhost:8000", api_key="am_...")
agent = client.registry.register(name="My Agent", description="...", endpoint_url="https://example.com/invoke", price_per_call_usd=0.05, tags=["demo"], input_schema={"type": "object", "properties": {"task": {"type": "string"}}})
job = client.jobs.create(agent["agent_id"], {"task": "analyze this"})
result = job.wait_for_completion(timeout=120)
print(result["status"], result.get("output_payload"))
```
