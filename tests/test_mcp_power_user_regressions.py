from aztea.mcp import meta_tools


def test_hire_async_forwards_single_job_max_price_cap(monkeypatch):
    def _fake_resolve(_session, _base, _hdrs, _timeout, args):
        return f"agent-{args.get('slug') or args.get('agent_id')}", None

    captured: dict[str, object] = {}

    def _fake_post(_session, _url, _hdrs, _timeout, body):
        captured["body"] = body
        return True, {"job_id": "job_async_cap", "status": "pending"}

    monkeypatch.setattr(meta_tools, "_resolve_agent_id", _fake_resolve)
    monkeypatch.setattr(meta_tools, "_post", _fake_post)
    ok, _ = meta_tools._hire_async(
        session=None,
        base="https://aztea.test",
        hdrs={},
        timeout=5,
        args={
            "slug": "python_executor",
            "input": {"code": "print(4)"},
            "max_price_cents": 2,
        },
    )
    assert ok is True
    assert captured["body"]["max_price_cents"] == 2


def test_schema_input_hint_returns_example_arguments():
    hint = meta_tools._schema_input_hint(
        {
            "type": "object",
            "required": ["content"],
            "properties": {
                "content": {"type": "string", "description": "Text to scan."},
                "min_entropy": {"type": "number", "default": 4.5},
                "mode": {"type": "string", "enum": ["fast", "deep"]},
            },
        }
    )
    assert hint["required_fields"] == ["content"]
    assert hint["fields"]["content"]["required"] is True
    assert hint["example_arguments"] == {
        "content": "<content>",
        "min_entropy": 4.5,
        "mode": "fast",
    }
