import subprocess


def handler(payload):
    subprocess.run(["echo", "pwned"], check=False)
    return {"ok": True}
