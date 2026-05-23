import importlib


def handler(payload):
    sp = importlib.import_module("subprocess")
    sp.run(["echo", "via importlib"])
    return {"ok": True}
