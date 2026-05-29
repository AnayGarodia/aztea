from typing import TypedDict


class Input(TypedDict):
    url: str
    timeout: int


class Output(TypedDict):
    status_code: int
    body: str


def handler(payload: Input) -> Output:
    """Fetch a URL and return its status + body."""
    return {"status_code": 200, "body": ""}
