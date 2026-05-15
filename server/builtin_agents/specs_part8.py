"""Built-in agent specs — batch 3 (ssl_certificate_decoder, diff_analyzer,
k8s_manifest_validator)."""

from __future__ import annotations

from server.builtin_agents.constants import (
    DIFF_ANALYZER_AGENT_ID,
    K8S_MANIFEST_VALIDATOR_AGENT_ID,
    SSL_CERTIFICATE_DECODER_AGENT_ID,
)


def load_builtin_specs_part8() -> list[dict]:
    """Return marketplace specs for the third batch of high-signal built-ins."""
    return [
        {
            "agent_id": SSL_CERTIFICATE_DECODER_AGENT_ID,
            "name": "SSL Certificate Decoder",
            "description": (
                "Decode PEM or base64-encoded DER X.509 certificates without making any "
                "network requests. Extracts subject and issuer distinguished names, SANs, "
                "key type and bit length, signature algorithm, validity dates, days "
                "remaining, key usage and extended key usage extensions, OCSP and CRL URLs, "
                "SHA-1 and SHA-256 fingerprints, CA flag, and self-signed detection. "
                "Accepts single certs or a batch of up to 10 for chain analysis."
            ),
            "endpoint_url": "internal://ssl_certificate_decoder",
            "price_per_call_usd": 0.002,
            "tags": ["ssl", "tls", "certificate", "security", "x509"],
            "is_featured": True,
            "match_keywords": [
                "x509",
                "certificate",
                "pem",
                "decode cert",
                "ssl cert",
                "tls certificate",
                "san",
                "subject alternative name",
                "certificate chain",
                "fingerprint",
            ],
            "block_keywords": ["fetch certificate from url", "check ssl of website"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "pem": {
                        "type": "string",
                        "description": "PEM-encoded certificate string.",
                    },
                    "der_base64": {
                        "type": "string",
                        "description": "Base64-encoded DER certificate.",
                    },
                    "pems": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Batch of PEM certs for chain analysis (max 10).",
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "subject": {"type": "object"},
                    "issuer": {"type": "object"},
                    "serial_number": {"type": "string"},
                    "version": {"type": "integer"},
                    "valid_from": {"type": "string"},
                    "valid_to": {"type": "string"},
                    "days_remaining": {"type": ["integer", "null"]},
                    "expired": {"type": "boolean"},
                    "not_yet_valid": {"type": "boolean"},
                    "san": {"type": "array", "items": {"type": "string"}},
                    "key_type": {"type": "string"},
                    "key_bits": {"type": ["integer", "null"]},
                    "key_curve": {"type": ["string", "null"]},
                    "signature_algorithm": {"type": "string"},
                    "key_usage": {"type": "array", "items": {"type": "string"}},
                    "extended_key_usage": {"type": "array", "items": {"type": "string"}},
                    "ocsp_urls": {"type": "array", "items": {"type": "string"}},
                    "fingerprints": {"type": "object"},
                    "is_ca": {"type": "boolean"},
                    "self_signed": {"type": "boolean"},
                },
                "required": ["subject", "valid_from", "valid_to", "expired", "fingerprints"],
            },
            "output_examples": [
                {
                    "input": {"pem": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----"},
                    "output": {
                        "subject": {"common_name": "example.com", "organization": "Example Inc."},
                        "issuer": {"common_name": "Let's Encrypt R3"},
                        "serial_number": "03:a1:b2:c3",
                        "version": 3,
                        "valid_from": "2026-01-01T00:00:00+00:00",
                        "valid_to": "2026-04-01T00:00:00+00:00",
                        "days_remaining": None,
                        "expired": True,
                        "not_yet_valid": False,
                        "san": ["DNS:example.com", "DNS:www.example.com"],
                        "key_type": "RSA",
                        "key_bits": 2048,
                        "key_curve": None,
                        "signature_algorithm": "sha256WithRSAEncryption",
                        "key_usage": ["digitalSignature", "keyEncipherment"],
                        "extended_key_usage": ["serverAuth"],
                        "ocsp_urls": ["http://r3.o.lencr.org"],
                        "fingerprints": {
                            "sha1": "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01",
                            "sha256": "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89",
                        },
                        "is_ca": False,
                        "self_signed": False,
                    },
                }
            ],
        },
        {
            "agent_id": DIFF_ANALYZER_AGENT_ID,
            "name": "Diff Analyzer",
            "description": (
                "Parse unified diff text (from git diff, git show, or similar) and produce "
                "structured change statistics with risk analysis. Reports per-file additions, "
                "deletions, file type classification, and risk flags including migration files, "
                "auth/payment/security code paths, removed tests, dependency changes, and "
                "potential secret additions (detected by pattern, value never returned). "
                "Computes an overall risk level: critical, high, medium, or low."
            ),
            "endpoint_url": "internal://diff_analyzer",
            "price_per_call_usd": 0.003,
            "tags": ["git", "diff", "code-review", "security", "risk-analysis"],
            "is_featured": True,
            "match_keywords": [
                "git diff",
                "diff analysis",
                "change risk",
                "pr review stats",
                "migration detection",
                "churn",
                "secret in diff",
                "diff stats",
            ],
            "block_keywords": ["explain the diff", "summarize the changes"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "diff": {
                        "type": "string",
                        "description": "Unified diff text (output of git diff or similar). Max 500KB.",
                    },
                },
                "required": ["diff"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "files_changed": {"type": "integer"},
                    "total_additions": {"type": "integer"},
                    "total_deletions": {"type": "integer"},
                    "total_churn": {"type": "integer"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "old_path": {"type": ["string", "null"]},
                                "status": {"type": "string"},
                                "additions": {"type": "integer"},
                                "deletions": {"type": "integer"},
                                "file_type": {"type": "string"},
                                "risk_flags": {"type": "array", "items": {"type": "string"}},
                                "binary": {"type": "boolean"},
                            },
                        },
                    },
                    "risk_summary": {
                        "type": "object",
                        "properties": {
                            "level": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                            "flags": {"type": "array", "items": {"type": "string"}},
                            "migration_files": {"type": "array", "items": {"type": "string"}},
                            "auth_changes": {"type": "boolean"},
                            "payment_changes": {"type": "boolean"},
                            "security_changes": {"type": "boolean"},
                            "test_additions": {"type": "integer"},
                            "test_deletions": {"type": "integer"},
                            "dependency_changes": {"type": "boolean"},
                            "secret_patterns_found": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "largest_files": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["files_changed", "total_additions", "total_deletions", "risk_summary"],
            },
            "output_examples": [
                {
                    "input": {"diff": "--- a/migrations/0042_add_payments.sql\n+++ b/migrations/0042_add_payments.sql\n@@ -0,0 +1,5 @@\n+CREATE TABLE payments (id INTEGER PRIMARY KEY);\n"},
                    "output": {
                        "files_changed": 1,
                        "total_additions": 5,
                        "total_deletions": 0,
                        "total_churn": 5,
                        "files": [
                            {
                                "path": "migrations/0042_add_payments.sql",
                                "old_path": None,
                                "status": "added",
                                "additions": 5,
                                "deletions": 0,
                                "file_type": "sql",
                                "risk_flags": ["migration", "payment_code"],
                                "binary": False,
                            }
                        ],
                        "risk_summary": {
                            "level": "critical",
                            "flags": ["migration_with_payment_code"],
                            "migration_files": ["migrations/0042_add_payments.sql"],
                            "auth_changes": False,
                            "payment_changes": True,
                            "security_changes": False,
                            "test_additions": 0,
                            "test_deletions": 0,
                            "dependency_changes": False,
                            "secret_patterns_found": [],
                        },
                        "largest_files": ["migrations/0042_add_payments.sql"],
                    },
                }
            ],
        },
        {
            "agent_id": K8S_MANIFEST_VALIDATOR_AGENT_ID,
            "name": "Kubernetes Manifest Validator",
            "description": (
                "Validate Kubernetes YAML manifests for structural correctness, security "
                "anti-patterns, and reliability gaps. Checks for unpinned image tags, "
                "missing resource limits and requests, privileged containers, hostNetwork / "
                "hostPID / hostIPC, runAsRoot, missing readiness probes, and missing security "
                "contexts. Runs kubectl --dry-run=client when kubectl is available in PATH for "
                "API-server-side validation. Always returns per-resource structured findings "
                "with severity (error/warning/info) and a JSON path for each issue."
            ),
            "endpoint_url": "internal://k8s_manifest_validator",
            "price_per_call_usd": 0.004,
            "tags": ["kubernetes", "k8s", "security", "devops", "yaml"],
            "is_featured": True,
            "match_keywords": [
                "kubernetes",
                "k8s",
                "kubectl",
                "manifest",
                "deployment yaml",
                "pod spec",
                "helm",
                "k8s security",
                "resource limits",
                "image tag",
            ],
            "block_keywords": [],
            "input_schema": {
                "type": "object",
                "properties": {
                    "manifest": {
                        "type": "string",
                        "description": "YAML text of one or more k8s resources (multi-doc with ---).",
                    },
                    "manifests": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of YAML manifest strings.",
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "valid": {"type": "boolean"},
                    "resources_parsed": {"type": "integer"},
                    "kubectl_available": {"type": "boolean"},
                    "kubectl_version": {"type": ["string", "null"]},
                    "resources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string"},
                                "api_version": {"type": "string"},
                                "name": {"type": ["string", "null"]},
                                "namespace": {"type": ["string", "null"]},
                                "index": {"type": "integer"},
                                "findings": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "severity": {"type": "string", "enum": ["error", "warning", "info"]},
                                            "rule": {"type": "string"},
                                            "message": {"type": "string"},
                                            "path": {"type": "string"},
                                        },
                                        "required": ["severity", "rule", "message", "path"],
                                    },
                                },
                            },
                        },
                    },
                    "total_findings": {"type": "integer"},
                    "by_severity": {"type": "object"},
                    "kubectl_output": {"type": ["string", "null"]},
                },
                "required": ["valid", "resources_parsed", "total_findings", "by_severity"],
            },
            "output_examples": [
                {
                    "input": {
                        "manifest": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  template:\n    spec:\n      containers:\n      - name: app\n        image: myapp:latest\n"
                    },
                    "output": {
                        "valid": False,
                        "resources_parsed": 1,
                        "kubectl_available": False,
                        "kubectl_version": None,
                        "resources": [
                            {
                                "kind": "Deployment",
                                "api_version": "apps/v1",
                                "name": "api",
                                "namespace": None,
                                "index": 0,
                                "findings": [
                                    {
                                        "severity": "warning",
                                        "rule": "image.unpinned",
                                        "message": "Container 'app' uses ':latest' tag — pin to a specific digest or version.",
                                        "path": "spec.template.spec.containers[0].image",
                                    },
                                    {
                                        "severity": "warning",
                                        "rule": "resources.no_limits",
                                        "message": "Container 'app' has no resource limits — set cpu and memory limits.",
                                        "path": "spec.template.spec.containers[0].resources",
                                    },
                                    {
                                        "severity": "warning",
                                        "rule": "reliability.no_readiness_probe",
                                        "message": "Container 'app' has no readinessProbe — traffic may reach unready pods.",
                                        "path": "spec.template.spec.containers[0].readinessProbe",
                                    },
                                ],
                            }
                        ],
                        "total_findings": 3,
                        "by_severity": {"error": 0, "warning": 3, "info": 0},
                        "kubectl_output": None,
                    },
                }
            ],
        },
    ]
