# OWNS: parsing Terraform plan JSON output and classifying resource changes by type and risk
# NOT OWNS: running Terraform, fetching state from remote backends, HCL parsing
# INVARIANTS: never executes any Terraform commands; pure JSON parsing only
# DECISIONS: supports both plan JSON (resource_changes) and state JSON (values.root_module) formats

"""
Aztea built-in agent: terraform_plan_analyzer

Parses `terraform plan -json` or `terraform show -json` output and returns
structured change analysis with per-resource risk classification. The JSON
schema complexity and risk rules make this impossible to fake accurately in chat.

Inputs:
  plan_json (str | dict, required) — JSON string or parsed dict from terraform

Outputs:
  Structured dict with summary, per-resource changes, and risk_summary.
"""

import json

MAX_PLAN_BYTES = 5 * 1024 * 1024  # 5 MB

_DB_PATTERNS: frozenset[str] = frozenset(
    ["db", "rds", "aurora", "postgres", "mysql", "dynamo", "redis",
     "elasticache", "mongo", "firestore", "bigquery", "datastore", "bigtable"]
)
_IAM_PATTERNS: frozenset[str] = frozenset(
    ["iam", "role", "policy", "permission", "acl", "access", "credential"]
)
_NETWORK_PATTERNS: frozenset[str] = frozenset(
    ["vpc", "subnet", "security_group", "firewall", "route", "nat",
     "gateway", "lb", "alb", "elb", "nlb"]
)
_STORAGE_PATTERNS: frozenset[str] = frozenset(
    ["bucket", "storage", "s3", "blob", "disk", "volume", "ebs", "efs"]
)

_REPLACE_ACTION_PAIRS: frozenset[tuple] = frozenset(
    [("delete", "create"), ("create", "delete")]
)


def _err(code: str, message: str, details: dict | None = None) -> dict:
    """Return a structured error envelope."""
    err: dict = {"code": f"terraform_plan_analyzer.{code}", "message": message}
    if details:
        err["details"] = details
    return {"error": err}


def _parse_input(payload: dict) -> tuple[dict, None] | tuple[None, dict]:
    """Validate and parse the plan_json input field.

    Returns (parsed_dict, None) on success or (None, error_dict) on failure.
    Enforced early so callers never deal with raw strings downstream.
    """
    raw = payload.get("plan_json")
    if raw is None:
        return None, _err("missing_plan", "plan_json is required")

    if isinstance(raw, dict):
        return raw, None

    if isinstance(raw, str):
        if len(raw.encode()) > MAX_PLAN_BYTES:
            return None, _err("plan_too_large", "plan_json exceeds the 5 MB limit")
        try:
            return json.loads(raw), None
        except json.JSONDecodeError as exc:
            # Quote the offending fragment back to the caller so they can
            # see which line of their input was bad. Tip: most JSON parse
            # errors fire on the line+col already in the exception, but we
            # also include the first 200 chars of `raw` so the caller can
            # see whether they passed `terraform show -json` output or
            # the human-readable `terraform plan` output by mistake.
            details = {"snippet": raw[:200], "passed_input_fragment": raw[:200]}
            return None, _err(
                "invalid_json",
                f"plan_json is not valid JSON: {exc}. Pass the output of "
                "`terraform show -json <plan_file>`, not the human-readable plan.",
                details,
            )

    return None, _err("invalid_json", "plan_json must be a JSON string or dict")


def _detect_format(plan: dict) -> str | None:
    """Return 'plan', 'state', or None if unrecognised."""
    if "resource_changes" in plan:
        return "plan"
    if "values" in plan and isinstance(plan.get("values"), dict):
        return "state"
    return None


def _map_actions(actions: list[str]) -> str:
    """Convert a Terraform actions list to a canonical action string."""
    if not actions:
        return "no-op"
    if actions == ["no-op"]:
        return "no-op"
    if actions == ["create"]:
        return "create"
    if actions == ["delete"]:
        return "delete"
    if actions == ["update"]:
        return "update"
    if actions == ["read"]:
        return "read"
    if tuple(actions) in _REPLACE_ACTION_PAIRS:
        return "replace"
    # Fallback for unexpected multi-action lists
    return "replace"


def _type_matches(resource_type: str, patterns: frozenset[str]) -> bool:
    """Return True if any pattern substring appears in the resource type."""
    lower = resource_type.lower()
    return any(p in lower for p in patterns)


def _classify_risk_flags(resource_type: str, action: str,
                          before_sensitive: bool, after_sensitive: bool) -> list[str]:
    """Build the list of risk flags for a single resource change."""
    flags: list[str] = []

    if action == "delete":
        flags.append("destroy")
    if action == "replace":
        flags.append("replace")

    is_db = _type_matches(resource_type, _DB_PATTERNS)
    is_iam = _type_matches(resource_type, _IAM_PATTERNS)
    is_network = _type_matches(resource_type, _NETWORK_PATTERNS)
    is_storage = _type_matches(resource_type, _STORAGE_PATTERNS)

    if is_db:
        flags.append("database_resource")
    if is_iam:
        flags.append("iam_resource")
    if is_network:
        flags.append("network_resource")
    if is_storage:
        flags.append("storage_resource")
    if is_db or is_storage:
        flags.append("stateful_resource")
    if before_sensitive or after_sensitive:
        flags.append("sensitive_data")

    return flags


def _risk_level(flags: list[str], action: str) -> str:
    """Derive the risk level from pre-computed flags and action.

    Priority order: critical > high > medium > low.
    """
    flag_set = set(flags)
    is_destructive = action in ("delete", "replace")
    is_stateful = "stateful_resource" in flag_set

    if is_destructive and is_stateful:
        return "critical"
    if is_destructive:
        return "high"
    if "iam_resource" in flag_set:
        return "high"
    if "network_resource" in flag_set and action == "delete":
        return "high"
    if action == "replace":
        return "medium"
    if "database_resource" in flag_set:
        return "medium"
    if "sensitive_data" in flag_set:
        return "medium"
    return "low"


def _sensitive_bool(value) -> bool:
    """Normalise before_sensitive / after_sensitive to bool.

    Terraform encodes this as either `false` or a nested dict of sensitive paths.
    """
    if value is False or value is None:
        return False
    if isinstance(value, dict):
        return bool(value)
    return bool(value)


def _parse_plan_resource(entry: dict) -> dict:
    """Extract a normalised change record from a plan resource_changes element."""
    change = entry.get("change", {})
    actions = change.get("actions", ["no-op"])
    action = _map_actions(actions)

    before_sensitive = _sensitive_bool(change.get("before_sensitive", False))
    after_sensitive = _sensitive_bool(change.get("after_sensitive", False))
    resource_type = entry.get("type", "")

    flags = _classify_risk_flags(resource_type, action, before_sensitive, after_sensitive)
    level = _risk_level(flags, action)

    address = entry.get("address", "")
    module_address = entry.get("module_address") or None
    if module_address and address.startswith(module_address + "."):
        local_address = address[len(module_address) + 1:]
    else:
        local_address = address

    return {
        "address": address,
        "module": module_address,
        "type": resource_type,
        "name": entry.get("name", local_address),
        "action": action,
        "provider": entry.get("provider_name") or None,
        "risk_level": level,
        "risk_flags": flags,
        "before_sensitive": before_sensitive,
        "after_sensitive": after_sensitive,
    }


def _collect_state_resources(module: dict) -> list[dict]:
    """Recursively collect resources from a state module node."""
    resources = list(module.get("resources", []))
    for child in module.get("child_modules", []):
        resources.extend(_collect_state_resources(child))
    return resources


def _parse_state_resources(plan: dict) -> list[dict]:
    """Build normalised change records from a terraform show -json state."""
    root = plan.get("values", {}).get("root_module", {})
    raw_resources = _collect_state_resources(root)
    result = []
    for res in raw_resources:
        resource_type = res.get("type", "")
        flags = _classify_risk_flags(resource_type, "no-op", False, False)
        level = _risk_level(flags, "no-op")
        result.append({
            "address": res.get("address", ""),
            "module": None,
            "type": resource_type,
            "name": res.get("name", ""),
            "action": "no-op",
            "provider": res.get("provider_name") or None,
            "risk_level": level,
            "risk_flags": flags,
            "before_sensitive": False,
            "after_sensitive": False,
        })
    return result


def _build_summary(changes: list[dict]) -> dict:
    """Aggregate per-action counts from the change list."""
    counts = {"to_add": 0, "to_change": 0, "to_destroy": 0,
              "to_replace": 0, "no_op": 0, "to_read": 0}
    action_map = {
        "create": "to_add",
        "update": "to_change",
        "delete": "to_destroy",
        "replace": "to_replace",
        "no-op": "no_op",
        "read": "to_read",
    }
    for ch in changes:
        key = action_map.get(ch["action"])
        if key:
            counts[key] += 1
    total = sum(counts.values())
    return {"total_changes": total, **counts}


def _build_risk_summary(changes: list[dict]) -> dict:
    """Derive the aggregated risk summary from all classified changes."""
    critical, high_risk, destroys, replaces = [], [], [], []
    db_changes, iam_changes, net_changes = [], [], []
    data_loss_risk = False

    for ch in changes:
        addr = ch["address"]
        flags = set(ch["risk_flags"])
        level = ch["risk_level"]
        action = ch["action"]

        if level == "critical":
            critical.append(addr)
        if level == "high":
            high_risk.append(addr)
        if action == "delete":
            destroys.append(addr)
        if action == "replace":
            replaces.append(addr)
        if "database_resource" in flags:
            db_changes.append(addr)
        if "iam_resource" in flags:
            iam_changes.append(addr)
        if "network_resource" in flags:
            net_changes.append(addr)
        if "stateful_resource" in flags and action in ("delete", "replace"):
            data_loss_risk = True

    if critical:
        overall = "critical"
    elif high_risk:
        overall = "high"
    elif any(ch["risk_level"] == "medium" for ch in changes):
        overall = "medium"
    else:
        overall = "low"

    return {
        "overall_risk": overall,
        "critical_resources": critical,
        "high_risk_resources": high_risk,
        "destroys": destroys,
        "replaces": replaces,
        "database_changes": db_changes,
        "iam_changes": iam_changes,
        "network_changes": net_changes,
        "data_loss_risk": data_loss_risk,
    }


def _tally_by_field(changes: list[dict], field: str) -> dict[str, int]:
    """Count changes grouped by a string field, dropping None values."""
    tally: dict[str, int] = {}
    for ch in changes:
        key = ch.get(field)
        if key is None:
            continue
        tally[key] = tally.get(key, 0) + 1
    return dict(sorted(tally.items()))


def run(payload: dict) -> dict:
    """Parse a Terraform plan or state JSON and return structured risk analysis.

    Pure JSON parsing only — never calls terraform or touches the filesystem.
    Supports both plan format (resource_changes) and state format (values.root_module).
    """
    plan, err = _parse_input(payload)
    if err is not None:
        return err

    fmt = _detect_format(plan)
    if fmt is None:
        return _err(
            "not_a_terraform_plan",
            "JSON does not contain 'resource_changes' (plan) or 'values' (state); "
            "run terraform plan -json or terraform show -json to produce valid input.",
        )

    terraform_version = plan.get("terraform_version") or None

    if fmt == "plan":
        raw_changes = plan.get("resource_changes", [])
        changes = [_parse_plan_resource(r) for r in raw_changes]
    else:
        changes = _parse_state_resources(plan)

    summary = _build_summary(changes)
    risk_summary = _build_risk_summary(changes)
    by_provider = _tally_by_field(changes, "provider")
    by_resource_type = _tally_by_field(changes, "type")

    return {
        "format": fmt,
        "terraform_version": terraform_version,
        "summary": summary,
        "changes": changes,
        "risk_summary": risk_summary,
        "by_provider": by_provider,
        "by_resource_type": by_resource_type,
    }
