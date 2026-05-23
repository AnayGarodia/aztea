def handler(payload):
    exec(payload.get("code", "1+1"))
    return {"ok": True}
