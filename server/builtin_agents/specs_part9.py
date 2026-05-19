"""Built-in agent specs — batch 4 (archive_inspector, unicode_inspector,
terraform_plan_analyzer)."""

from __future__ import annotations

from server.builtin_agents.constants import (
    ARCHIVE_INSPECTOR_AGENT_ID,
    TERRAFORM_PLAN_ANALYZER_AGENT_ID,
    UNICODE_INSPECTOR_AGENT_ID,
)


def load_builtin_specs_part9() -> list[dict]:
    """Return marketplace specs for the fourth batch of high-signal built-ins."""
    return [
        {
            "agent_id": ARCHIVE_INSPECTOR_AGENT_ID,
            "name": "Archive Inspector",
            "description": (
                "Inspect ZIP and tar archives (including .tar.gz, .tar.bz2, .tar.xz) "
                "without extracting files to disk. Returns full entry listings with "
                "sizes, modes, and modification times, plus security analysis: zip bomb "
                "detection (compression ratio > 100x), path traversal entries (.. in paths), "
                "absolute paths, suspicious executable extensions, symlinks, and deeply "
                "nested paths. Accepts base64-encoded archive bytes. Pure stdlib — no deps."
            ),
            "endpoint_url": "internal://archive_inspector",
            "price_per_call_usd": 0.01,
            "tags": ["archive", "zip", "tar", "security", "file-inspection"],
            "is_featured": True,
            "match_keywords": [
                "zip", "tar", "archive", "zip bomb", "path traversal",
                "inspect archive", "list zip contents", "tarball",
            ],
            "block_keywords": ["extract archive", "unzip", "decompress"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "content_base64": {
                        "type": "string",
                        "description": "Base64-encoded archive bytes (ZIP or tar). Max 50MB decoded.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional filename hint for format detection (e.g. 'build.tar.gz').",
                    },
                    "max_entries": {
                        "type": "integer",
                        "description": "Max entries to list (default 500).",
                    },
                },
                "required": ["content_base64"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "format": {"type": "string"},
                    "total_entries": {"type": "integer"},
                    "total_uncompressed_bytes": {"type": "integer"},
                    "total_compressed_bytes": {"type": "integer"},
                    "compression_ratio": {"type": ["number", "null"]},
                    "entries": {"type": "array"},
                    "truncated": {"type": "boolean"},
                    "security": {
                        "type": "object",
                        "properties": {
                            "zip_bomb_risk": {"type": "boolean"},
                            "path_traversal_entries": {"type": "array"},
                            "absolute_path_entries": {"type": "array"},
                            "suspicious_extensions": {"type": "array"},
                            "symlink_entries": {"type": "array"},
                            "deeply_nested_entries": {"type": "array"},
                        },
                    },
                    "largest_entries": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["format", "total_entries", "security"],
            },
            "output_examples": [
                {
                    "input": {"content_base64": "<base64-encoded zip>", "filename": "build.zip"},
                    "output": {
                        "format": "zip",
                        "total_entries": 12,
                        "total_uncompressed_bytes": 45678,
                        "total_compressed_bytes": 8234,
                        "compression_ratio": 5.55,
                        "entries": [
                            {"path": "dist/index.js", "size_bytes": 24000, "compressed_bytes": 4200,
                             "is_dir": False, "mode": "0o644", "modified": "2026-05-01T12:00:00",
                             "symlink_target": None},
                        ],
                        "truncated": False,
                        "security": {
                            "zip_bomb_risk": False,
                            "path_traversal_entries": [],
                            "absolute_path_entries": [],
                            "suspicious_extensions": [],
                            "symlink_entries": [],
                            "deeply_nested_entries": [],
                        },
                        "largest_entries": ["dist/index.js"],
                    },
                }
            ],
        },
        {
            "agent_id": UNICODE_INSPECTOR_AGENT_ID,
            "name": "Unicode Inspector",
            "description": (
                "Inspect Unicode strings for character properties, invisible characters, "
                "bidirectional text control codes (used in 'Trojan source' attacks), "
                "homoglyph risks (Cyrillic/Greek chars that look like Latin), mixed-script "
                "detection, private use characters, and normalization form analysis (NFC/NFD/"
                "NFKC/NFKD). Returns per-character breakdown with codepoints, names, and "
                "categories. Pure stdlib — uses only Python's unicodedata module."
            ),
            "endpoint_url": "internal://unicode_inspector",
            "price_per_call_usd": 0.01,
            "tags": ["unicode", "security", "encoding", "homoglyph", "bidi"],
            "is_featured": True,
            "match_keywords": [
                "unicode", "codepoint", "homoglyph", "invisible character",
                "bidi attack", "trojan source", "mixed script", "normalization",
                "unicode security", "zero width",
            ],
            "block_keywords": [],
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "String to inspect. Max 10,000 chars.",
                    },
                    "texts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Batch of strings (max 20).",
                    },
                },
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "length_chars": {"type": "integer"},
                    "length_bytes_utf8": {"type": "integer"},
                    "scripts_detected": {"type": "array", "items": {"type": "string"}},
                    "categories": {"type": "object"},
                    "characters": {"type": "array"},
                    "normalization": {
                        "type": "object",
                        "properties": {
                            "is_nfc": {"type": "boolean"},
                            "is_nfd": {"type": "boolean"},
                            "is_nfkc": {"type": "boolean"},
                            "is_nfkd": {"type": "boolean"},
                            "nfc_differs": {"type": "boolean"},
                            "nfkc_differs": {"type": "boolean"},
                        },
                    },
                    "security": {
                        "type": "object",
                        "properties": {
                            "has_invisible_chars": {"type": "boolean"},
                            "invisible_chars": {"type": "array"},
                            "has_bidi_controls": {"type": "boolean"},
                            "bidi_controls": {"type": "array"},
                            "has_mixed_scripts": {"type": "boolean"},
                            "mixed_script_risk": {"type": "string"},
                            "homoglyph_suspicious_pairs": {"type": "array"},
                        },
                    },
                },
                "required": ["length_chars", "scripts_detected", "security"],
            },
            "output_examples": [
                {
                    "input": {"text": "pаypal.com"},
                    "output": {
                        "length_chars": 10,
                        "length_bytes_utf8": 11,
                        "scripts_detected": ["LATIN", "CYRILLIC", "COMMON"],
                        "categories": {"Ll": 8, "Po": 1, "Lo": 1},
                        "normalization": {"is_nfc": True, "is_nfd": False,
                                          "is_nfkc": True, "is_nfkd": False,
                                          "nfc_differs": False, "nfkc_differs": False},
                        "security": {
                            "has_invisible_chars": False,
                            "invisible_chars": [],
                            "has_bidi_controls": False,
                            "bidi_controls": [],
                            "has_mixed_scripts": True,
                            "mixed_script_risk": "high",
                            "homoglyph_suspicious_pairs": [
                                {"char": "а", "looks_like": "a", "codepoint": "U+0430"}
                            ],
                        },
                    },
                }
            ],
        },
        {
            "agent_id": TERRAFORM_PLAN_ANALYZER_AGENT_ID,
            "name": "Terraform Plan Analyzer",
            "description": (
                "Parse the JSON output of 'terraform plan -json' or 'terraform show -json' "
                "and produce structured change analysis. Classifies each resource change as "
                "create, update, delete, replace, read, or no-op. Flags risky changes: "
                "destroys and replaces of stateful resources (databases, storage) are "
                "critical; IAM and network changes are high risk. Returns per-provider and "
                "per-resource-type counts. Supports both plan format (resource_changes) and "
                "state format (values.root_module). Pure JSON parsing — never runs Terraform."
            ),
            "endpoint_url": "internal://terraform_plan_analyzer",
            "price_per_call_usd": 0.01,
            "tags": ["terraform", "infrastructure", "iac", "devops", "risk-analysis"],
            "is_featured": True,
            "match_keywords": [
                "terraform", "terraform plan", "terraform show",
                "infrastructure change", "iac review", "resource destroy",
                "terraform risk", "plan json",
            ],
            "block_keywords": ["run terraform", "apply terraform"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "plan_json": {
                        "oneOf": [{"type": "string"}, {"type": "object"}],
                        "description": "JSON string or parsed dict from 'terraform plan -json'. Max 5MB.",
                    },
                },
                "required": ["plan_json"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["plan", "state"]},
                    "terraform_version": {"type": ["string", "null"]},
                    "summary": {
                        "type": "object",
                        "properties": {
                            "total_changes": {"type": "integer"},
                            "to_add": {"type": "integer"},
                            "to_change": {"type": "integer"},
                            "to_destroy": {"type": "integer"},
                            "to_replace": {"type": "integer"},
                            "no_op": {"type": "integer"},
                            "to_read": {"type": "integer"},
                        },
                    },
                    "changes": {"type": "array"},
                    "risk_summary": {
                        "type": "object",
                        "properties": {
                            "overall_risk": {"type": "string"},
                            "destroys": {"type": "array"},
                            "replaces": {"type": "array"},
                            "data_loss_risk": {"type": "boolean"},
                        },
                    },
                    "by_provider": {"type": "object"},
                    "by_resource_type": {"type": "object"},
                },
                "required": ["format", "summary", "changes", "risk_summary"],
            },
            "output_examples": [
                {
                    "input": {"plan_json": "{\"format_version\":\"1.0\",\"terraform_version\":\"1.7.0\",\"resource_changes\":[{\"address\":\"aws_db_instance.main\",\"type\":\"aws_db_instance\",\"name\":\"main\",\"provider_name\":\"registry.terraform.io/hashicorp/aws\",\"change\":{\"actions\":[\"delete\"],\"before\":{\"identifier\":\"prod\"},\"after\":null,\"before_sensitive\":false,\"after_sensitive\":false}}]}"},
                    "output": {
                        "format": "plan",
                        "terraform_version": "1.7.0",
                        "summary": {"total_changes": 1, "to_add": 0, "to_change": 0,
                                    "to_destroy": 1, "to_replace": 0, "no_op": 0, "to_read": 0},
                        "changes": [{
                            "address": "aws_db_instance.main",
                            "module": None,
                            "type": "aws_db_instance",
                            "name": "main",
                            "action": "delete",
                            "provider": "registry.terraform.io/hashicorp/aws",
                            "risk_level": "critical",
                            "risk_flags": ["destroy", "database_resource", "stateful_resource"],
                            "before_sensitive": False,
                            "after_sensitive": False,
                        }],
                        "risk_summary": {
                            "overall_risk": "critical",
                            "critical_resources": ["aws_db_instance.main"],
                            "high_risk_resources": [],
                            "destroys": ["aws_db_instance.main"],
                            "replaces": [],
                            "database_changes": ["aws_db_instance.main"],
                            "iam_changes": [],
                            "network_changes": [],
                            "data_loss_risk": True,
                        },
                        "by_provider": {"registry.terraform.io/hashicorp/aws": 1},
                        "by_resource_type": {"aws_db_instance": 1},
                    },
                }
            ],
        },
    ]
