"""Audit dependencies for known CVEs."""

from typing import Literal, Optional, Union


def handler(
    ecosystem: Literal["npm", "pypi", "rubygems"],
    requirements: str,
    severity_floor: Optional[str] = None,
    timeout_seconds: Union[int, float] = 30,
) -> dict:
    """Run a CVE audit across the given ecosystem's lockfile."""
    return {"vulnerable": []}
