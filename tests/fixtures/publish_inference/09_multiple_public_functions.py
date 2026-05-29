"""Two public functions, no clear `handler`. Expect ambiguity in the spec."""


def scan_files(path: str) -> dict:
    return {"path": path}


def lint_files(path: str) -> dict:
    return {"path": path}
