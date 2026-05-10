#!/usr/bin/env python3
"""CLI for managing workspace-context consent: ``aztea workspace [...]``.

# OWNS: User-facing commands for approving, denying, listing, and forgetting
#       per-directory consent decisions for workspace-context sharing.
# NOT OWNS: Bundle construction (core/workspace_bundle.py) or backend wiring.
# INVARIANTS:
#   - All output is plain text on stdout / stderr — no JSON, so it pipes
#     cleanly into shell tools and never confuses an MCP transport.
#   - Exit code is 0 on success, 1 on user error, 2 on system error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core import workspace_bundle as _wb  # noqa: E402
from core import workspace_consent as _wc  # noqa: E402

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_SYSTEM_ERROR = 2


def _resolve_target(arg: str | None) -> str:
    """Return the path the user is operating on. Defaults to cwd."""
    return os.path.realpath(arg or os.getcwd())


def _cmd_approve(args: argparse.Namespace) -> int:
    target = _resolve_target(args.path)
    if not os.path.isdir(target):
        print(f"error: not a directory: {target}", file=sys.stderr)
        return EXIT_USER_ERROR
    _wc.approve(target)
    print(f"Approved workspace context for: {target}")
    print("Aztea agents will now receive a ~5KB summary of this directory")
    print("(file tree, manifests, README excerpt) on every call from here.")
    return EXIT_OK


def _cmd_deny(args: argparse.Namespace) -> int:
    target = _resolve_target(args.path)
    _wc.deny(target)
    print(f"Denied workspace context for: {target}")
    print("Aztea agents will not receive any summary of this directory.")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    target = _resolve_target(args.path)
    state = _wc.get_state(target)
    print(f"Path:   {target}")
    print(f"State:  {state}")
    if state == "approved":
        try:
            bundle = _wb.build_light_bundle(target)
        except (ValueError, OSError) as exc:
            print(f"(could not build bundle preview: {exc})")
            return EXIT_OK
        print(f"Branch: {bundle.git_branch or '(detached or no git)'}")
        print(f"Manifests detected: {', '.join(sorted(bundle.manifests.keys())) or '(none)'}")
        print(f"README excerpt:     {'yes' if bundle.readme_excerpt else 'no'}")
        print(f"Truncated:          {bundle.truncated}")
        print(f"Fingerprint:        {bundle.bundle_fingerprint[:12]}...")
    return EXIT_OK


def _cmd_list(_args: argparse.Namespace) -> int:
    rows = _wc.list_all()
    if not rows:
        print("(no workspace consent decisions recorded)")
        return EXIT_OK
    width = max(len(str(row["path"])) for row in rows)
    for row in rows:
        timestamp = row.get("approved_at") or row.get("denied_at") or ""
        print(f"{str(row['state']):<10} {str(row['path']):<{width}}  {timestamp}")
    return EXIT_OK


def _cmd_forget(args: argparse.Namespace) -> int:
    target = _resolve_target(args.path)
    removed = _wc.forget(target)
    if removed:
        print(f"Forgot workspace consent for: {target}")
    else:
        print(f"No prior decision recorded for: {target}")
    return EXIT_OK


def _cmd_preview(args: argparse.Namespace) -> int:
    target = _resolve_target(args.path)
    if not os.path.isdir(target):
        print(f"error: not a directory: {target}", file=sys.stderr)
        return EXIT_USER_ERROR
    try:
        bundle = _wb.build_light_bundle(target)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_SYSTEM_ERROR
    print(f"# Workspace bundle for: {target}")
    print(f"# Branch: {bundle.git_branch or '(none)'}")
    print(f"# Truncated: {bundle.truncated}")
    print(f"# Fingerprint: {bundle.bundle_fingerprint}")
    print()
    print("## File tree")
    print(bundle.file_tree)
    print()
    print(f"## Manifests: {sorted(bundle.manifests.keys())}")
    print()
    if bundle.readme_excerpt:
        print("## README (first lines)")
        print("\n".join(bundle.readme_excerpt.splitlines()[:20]))
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aztea workspace",
        description="Manage per-directory consent for sharing workspace context with Aztea agents.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, func, helptext in (
        ("approve", _cmd_approve, "Approve sharing workspace context from this directory."),
        ("deny", _cmd_deny, "Deny sharing workspace context from this directory."),
        ("status", _cmd_status, "Show the consent state for this directory."),
        ("forget", _cmd_forget, "Remove the recorded decision for this directory."),
        ("preview", _cmd_preview, "Print the bundle that would be shared (no upload)."),
    ):
        cmd_parser = sub.add_parser(name, help=helptext)
        cmd_parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Directory to operate on (default: current working directory).",
        )
        cmd_parser.set_defaults(func=func)
    list_parser = sub.add_parser("list", help="List all recorded consent decisions.")
    list_parser.set_defaults(func=_cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
