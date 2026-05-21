# OWNS: structural validation of Kubernetes YAML manifests and kubectl dry-run execution
# NOT OWNS: applying manifests to clusters, Helm chart rendering, OPA/Gatekeeper policy
# INVARIANTS: never applies manifests to any cluster; dry-run only; no kubeconfig writes
# DECISIONS: always runs structural checks regardless of kubectl availability; kubectl is additive

import shutil
import subprocess

import yaml

MAX_INPUT_BYTES = 200_000
MAX_RESOURCES = 50
KUBECTL_TIMEOUT = 15

# Kinds that carry a pod spec (directly or nested)
_POD_SPEC_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}
_JOB_KINDS = {"Job"}
_CRON_KINDS = {"CronJob"}
_ALL_POD_KINDS = _POD_SPEC_KINDS | _JOB_KINDS | _CRON_KINDS | {"Pod"}

_FINDING_SEVERITIES = ("error", "warning", "info")


def run(payload: dict) -> dict:
    """Validate Kubernetes YAML manifests structurally and via kubectl --dry-run=client.

    Structural checks always run; kubectl is additive when available on PATH.
    Returns a findings envelope — never applies anything to a real cluster.
    """
    raw_manifests = _collect_raw_manifests(payload)
    if "error" in raw_manifests:
        return raw_manifests

    combined_yaml, parse_error = _join_manifests(raw_manifests["strings"])
    if parse_error:
        return parse_error

    docs, parse_error = _parse_yaml(combined_yaml)
    if parse_error:
        return parse_error

    size_error = _check_limits(raw_manifests["strings"], docs)
    if size_error:
        return size_error

    resources = [_analyse_resource(doc, idx) for idx, doc in enumerate(docs)]
    kubectl_available, kubectl_version, kubectl_output = _run_kubectl(combined_yaml)

    if kubectl_output:
        _attach_kubectl_findings(kubectl_output, resources)

    return _build_response(resources, kubectl_available, kubectl_version, kubectl_output)


# ---------------------------------------------------------------------------
# Input collection
# ---------------------------------------------------------------------------

def _collect_raw_manifests(payload: dict) -> dict:
    """Return {"strings": [str, ...]} or {"error": ...}."""
    manifest = payload.get("manifest")
    manifests = payload.get("manifests")

    if manifest is None and not manifests:
        return _error("k8s_manifest_validator.missing_manifest", "No manifest provided.")

    strings = []
    if manifest:
        strings.append(str(manifest))
    if manifests:
        strings.extend(str(m) for m in manifests)

    return {"strings": strings}


def _join_manifests(strings: list) -> tuple:
    """Concatenate manifest strings with --- separators. Returns (combined, None) or (None, error)."""
    combined = "\n---\n".join(strings)
    if len(combined.encode()) > MAX_INPUT_BYTES:
        return None, _error(
            "k8s_manifest_validator.input_too_large",
            f"Combined manifest exceeds {MAX_INPUT_BYTES // 1000}KB limit.",
        )
    return combined, None


def _parse_yaml(combined: str) -> tuple:
    """Parse combined YAML into a list of non-empty docs. Returns (docs, None) or (None, error)."""
    try:
        docs = [d for d in yaml.safe_load_all(combined) if d is not None]
        return docs, None
    except yaml.YAMLError as exc:
        # Surface the offending fragment so callers can see which line is bad.
        return None, _error(
            "k8s_manifest_validator.invalid_yaml",
            f"YAML parse error: {exc}",
            {"passed_input_fragment": combined[:200]},
        )


def _check_limits(strings: list, docs: list) -> dict | None:
    """Return a size-limit error dict or None."""
    if len(docs) > MAX_RESOURCES:
        return _error(
            "k8s_manifest_validator.too_many_resources",
            f"Parsed {len(docs)} resources; limit is {MAX_RESOURCES}.",
        )
    return None


# ---------------------------------------------------------------------------
# Per-resource analysis
# ---------------------------------------------------------------------------

def _analyse_resource(doc: dict, idx: int) -> dict:
    """Run all applicable structural rules against a single parsed resource."""
    findings = []
    findings.extend(_check_required_fields(doc))
    findings.extend(_check_required_metadata(doc))

    kind = doc.get("kind", "")
    if kind in _ALL_POD_KINDS:
        pod_spec = _extract_pod_spec(doc, kind)
        if pod_spec:
            findings.extend(_check_pod_spec(pod_spec))

    if kind == "Service":
        findings.extend(_check_service(doc))

    if kind in _CRON_KINDS:
        findings.extend(_check_cronjob(doc))

    meta = doc.get("metadata") or {}
    return {
        "kind": kind,
        "api_version": doc.get("apiVersion", ""),
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "index": idx,
        "findings": findings,
    }


def _extract_pod_spec(resource: dict, kind: str) -> dict | None:
    """Return the pod spec dict for a resource, or None if unavailable.

    1.7.0 bug fix: Pre-1.7.0 the Pod branch returned the WHOLE resource
    (`{apiVersion, kind, metadata, spec, ...}`) instead of `resource.spec`.
    Every downstream check then looked at the wrong nesting level, so
    `spec.securityContext.privileged: true` slipped past every rule and
    only the "no securityContext" info finding fired. Other resource
    kinds (Deployment / Job / CronJob) correctly reach into `.spec...`,
    so the bug was Pod-specific.
    """
    spec = resource.get("spec") or {}
    if kind == "Pod":
        return spec  # was `resource` — wrong nesting since at least 2026-04
    if kind in _POD_SPEC_KINDS:
        return (spec.get("template") or {}).get("spec")
    if kind in _JOB_KINDS:
        return (spec.get("template") or {}).get("spec")
    if kind in _CRON_KINDS:
        job_spec = (spec.get("jobTemplate") or {}).get("spec") or {}
        return (job_spec.get("template") or {}).get("spec")
    return None


# ---------------------------------------------------------------------------
# Structural rule checkers — one concern per function
# ---------------------------------------------------------------------------

def _check_required_fields(doc: dict) -> list:
    """Verify apiVersion and kind are present."""
    findings = []
    if not doc.get("apiVersion"):
        findings.append(_finding("error", "required_fields", "apiVersion is required.", "apiVersion"))
    if not doc.get("kind"):
        findings.append(_finding("error", "required_fields", "kind is required.", "kind"))
    return findings


def _check_required_metadata(doc: dict) -> list:
    """Warn when metadata.name is absent."""
    meta = doc.get("metadata") or {}
    if not meta.get("name"):
        return [_finding("warning", "required_metadata", "metadata.name should be present.", "metadata.name")]
    return []


def _check_pod_spec(pod_spec: dict) -> list:
    """Run all pod-spec rules; return combined findings list."""
    findings = []
    containers = (pod_spec.get("containers") or []) + (pod_spec.get("initContainers") or [])
    for i, container in enumerate(containers):
        prefix = f"spec.containers[{i}]"
        findings.extend(_check_container(container, prefix))
    findings.extend(_check_host_flags(pod_spec))
    findings.extend(_check_automount_token(pod_spec))
    findings.extend(_check_pod_security_context(pod_spec))
    findings.extend(_check_readiness_probes(pod_spec))
    return findings


def _check_container(container: dict, prefix: str) -> list:
    """Validate a single container dict."""
    findings = []
    image = container.get("image", "")
    if not image or image.endswith(":latest") or (":" not in image):
        findings.append(_finding("warning", "image.unpinned", "Pin your image tag.", f"{prefix}.image"))

    resources = container.get("resources") or {}
    limits = resources.get("limits") or {}
    requests = resources.get("requests") or {}

    if not limits.get("cpu") or not limits.get("memory"):
        findings.append(_finding(
            "warning", "resources.no_limits",
            "Container is missing resources.limits.cpu or resources.limits.memory.",
            f"{prefix}.resources.limits",
        ))
    if not requests:
        findings.append(_finding(
            "info", "resources.no_requests",
            "Container is missing resources.requests.",
            f"{prefix}.resources.requests",
        ))

    sc = container.get("securityContext") or {}
    if sc.get("privileged") is True:
        findings.append(_finding(
            "error", "security.privileged",
            "Privileged container is not allowed.",
            f"{prefix}.securityContext.privileged",
        ))
    if sc.get("runAsUser") == 0 or sc.get("runAsNonRoot") is False:
        # CIS Kubernetes Benchmark 5.2.6: containers should not run as
        # root. Pre-1.7.0 this was a `warning`; bumping to `error` so
        # the finding surfaces in summary counts that ignore warnings
        # (e.g. blocking PR-comment renderers).
        findings.append(_finding(
            "error", "security.runAsRoot",
            "Container runs as root (runAsUser:0 or runAsNonRoot:false). Set runAsNonRoot:true and a non-zero runAsUser.",
            f"{prefix}.securityContext",
        ))
    if not sc:
        findings.append(_finding(
            "info", "security.no_context",
            "No securityContext set on container.",
            f"{prefix}.securityContext",
        ))
    return findings


def _check_host_flags(pod_spec: dict) -> list:
    """Check hostNetwork, hostPID, hostIPC flags."""
    findings = []
    flag_map = {
        "hostNetwork": "security.hostNetwork",
        "hostPID": "security.hostPID",
        "hostIPC": "security.hostIPC",
    }
    for field, rule in flag_map.items():
        if pod_spec.get(field) is True:
            findings.append(_finding("error", rule, f"spec.{field}: true is not allowed.", f"spec.{field}"))
    return findings


def _check_automount_token(pod_spec: dict) -> list:
    """Warn when automountServiceAccountToken is explicitly true."""
    if pod_spec.get("automountServiceAccountToken") is True:
        return [_finding(
            "info", "security.automount_token",
            "automountServiceAccountToken is explicitly true; disable unless required.",
            "spec.automountServiceAccountToken",
        )]
    return []


def _check_pod_security_context(pod_spec: dict) -> list:
    """Pod-level security findings.

    1.7.0: pre-existing version only flagged the ABSENCE of a
    securityContext as info. A pod with privileged:true / runAsUser:0
    declared at the POD level (not the container) slipped through with
    no error finding because every other privileged/root rule lived in
    `_check_container`. Now we duplicate the rules at pod scope so a
    K8s manifest like:
      spec:
        securityContext: {runAsUser: 0}
        containers: [{name: c, image: nginx, securityContext: {privileged: true}}]
    surfaces both as `error` findings, not just `info: no_context`.
    """
    sc = pod_spec.get("securityContext") or {}
    if not sc:
        return [_finding(
            "info", "security.no_context",
            "No pod-level securityContext set.",
            "spec.securityContext",
        )]
    findings: list = []
    if sc.get("privileged") is True:
        findings.append(_finding(
            "error", "security.pod.privileged",
            "Pod-level privileged is set — every container inherits root capabilities.",
            "spec.securityContext.privileged",
        ))
    if sc.get("runAsUser") == 0 or sc.get("runAsNonRoot") is False:
        findings.append(_finding(
            "warning", "security.pod.runAsRoot",
            "Pod runs as root — set runAsNonRoot:true and a non-zero runAsUser.",
            "spec.securityContext",
        ))
    if sc.get("hostPID") is True or sc.get("hostNetwork") is True or sc.get("hostIPC") is True:
        findings.append(_finding(
            "error", "security.pod.host_namespaces",
            "Pod shares host namespaces (hostNetwork/hostPID/hostIPC).",
            "spec.securityContext",
        ))
    return findings


def _check_readiness_probes(pod_spec: dict) -> list:
    """Warn when Deployment containers lack a readinessProbe."""
    findings = []
    containers = pod_spec.get("containers") or []
    for i, container in enumerate(containers):
        if not container.get("readinessProbe"):
            findings.append(_finding(
                "warning", "reliability.no_readiness_probe",
                "Container has no readinessProbe; add one for safe rolling updates.",
                f"spec.containers[{i}].readinessProbe",
            ))
    return findings


def _check_service(resource: dict) -> list:
    """Validate NodePort range for Service resources."""
    spec = resource.get("spec") or {}
    if spec.get("type") != "NodePort":
        return []
    findings = []
    for port in spec.get("ports") or []:
        np = port.get("nodePort")
        if np is not None and not (30000 <= np <= 32767):
            findings.append(_finding(
                "error", "service.nodeport_range",
                f"nodePort {np} is outside the valid range 30000–32767.",
                "spec.ports[].nodePort",
            ))
    return findings


def _check_cronjob(resource: dict) -> list:
    """Warn when CronJob has no concurrencyPolicy."""
    spec = resource.get("spec") or {}
    if not spec.get("concurrencyPolicy"):
        return [_finding(
            "warning", "cronjob.no_concurrency_policy",
            "CronJob has no concurrencyPolicy; set Forbid or Replace to avoid overlaps.",
            "spec.concurrencyPolicy",
        )]
    return []


# ---------------------------------------------------------------------------
# kubectl dry-run
# ---------------------------------------------------------------------------

def _run_kubectl(combined_yaml: str) -> tuple:
    """Run kubectl apply --dry-run=client. Returns (available, version, output_str)."""
    kubectl = shutil.which("kubectl")
    if not kubectl:
        return False, None, None

    version = _kubectl_version(kubectl)
    try:
        result = subprocess.run(
            [kubectl, "apply", "--dry-run=client", "-f", "-"],
            input=combined_yaml,
            capture_output=True,
            text=True,
            timeout=KUBECTL_TIMEOUT,
        )
        output = (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        output = f"kubectl timed out after {KUBECTL_TIMEOUT}s"
    except OSError as exc:
        output = f"kubectl exec error: {exc}"

    return True, version, output or None


def _kubectl_version(kubectl_path: str) -> str | None:
    """Return a short kubectl client version string or None."""
    try:
        result = subprocess.run(
            [kubectl_path, "version", "--client", "--short"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or result.stderr.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _attach_kubectl_findings(kubectl_output: str, resources: list) -> None:
    """Parse kubectl stderr error lines and append findings to the first resource."""
    error_lines = [ln for ln in kubectl_output.splitlines() if "error" in ln.lower()]
    if not error_lines or not resources:
        return
    target = resources[0]
    for line in error_lines:
        target["findings"].append(
            _finding("error", "kubectl.dry_run", line.strip(), "")
        )


# ---------------------------------------------------------------------------
# Response assembly
# ---------------------------------------------------------------------------

def _build_response(
    resources: list,
    kubectl_available: bool,
    kubectl_version: str | None,
    kubectl_output: str | None,
) -> dict:
    """Assemble the final output envelope."""
    by_severity = {"error": 0, "warning": 0, "info": 0}
    for res in resources:
        for f in res["findings"]:
            sev = f["severity"]
            if sev in by_severity:
                by_severity[sev] += 1

    total = sum(by_severity.values())
    return {
        "valid": by_severity["error"] == 0,
        "resources_parsed": len(resources),
        "kubectl_available": kubectl_available,
        "kubectl_version": kubectl_version,
        "resources": resources,
        "total_findings": total,
        "by_severity": by_severity,
        "kubectl_output": kubectl_output,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding(severity: str, rule: str, message: str, path: str) -> dict:
    """Construct a single finding dict."""
    return {"severity": severity, "rule": rule, "message": message, "path": path}


def _error(code: str, message: str, details: dict | None = None) -> dict:
    """Construct a structured error envelope."""
    err: dict = {"code": code, "message": message}
    if details:
        err["details"] = details
    return {"error": err}
