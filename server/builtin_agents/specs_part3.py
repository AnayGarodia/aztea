"""Third chunk of built-in agent specs — 2026-05-18 catalog-gap fill.

Six new agents addressing gaps surfaced in the 2026-05-18 test report:
regex_tester, jwt_validator, sbom_generator, pypi_metadata,
github_releases, hcl_terraform_analyzer.
"""

from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
)
from server.builtin_agents.constants import (
    REGEX_TESTER_AGENT_ID as _REGEX_TESTER_AGENT_ID,
    JWT_VALIDATOR_AGENT_ID as _JWT_VALIDATOR_AGENT_ID,
    SBOM_GENERATOR_AGENT_ID as _SBOM_GENERATOR_AGENT_ID,
    PYPI_METADATA_AGENT_ID as _PYPI_METADATA_AGENT_ID,
    GITHUB_RELEASES_AGENT_ID as _GITHUB_RELEASES_AGENT_ID,
    HCL_TERRAFORM_ANALYZER_AGENT_ID as _HCL_TERRAFORM_ANALYZER_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object


def _regex_tester_spec() -> dict[str, Any]:
    return {
        "agent_id": _REGEX_TESTER_AGENT_ID,
        "name": "Regex Tester",
        "description": (
            "Use when the user wants to test a Python regex against one "
            "or more strings. Compiles the pattern with the real "
            "``re`` engine, returns per-string matches with spans and "
            "groups, and reports compile errors structured instead of "
            "raising. Useful for verifying regex behavior without "
            "spinning up a code executor."
        ),
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_REGEX_TESTER_AGENT_ID],
        "price_per_call_usd": 0.01,
        "tags": ["regex", "pattern", "developer-tools"],
        "match_keywords": [
            "regex", "regexp", "regular expression", "regex test",
            "test pattern", "match pattern", "re.match", "re.findall",
        ],
        "block_keywords": ["audit", "credit card", "jwt", "joke"],
        "kind": "aztea_built",
        "category": "Developer Tools",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Python re-compatible regex pattern.",
                    "example": r"\d+",
                },
                "test_string": {
                    "type": "string",
                    "description": "Single string to test against.",
                },
                "test_strings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alternative to test_string: multiple strings.",
                },
                "flags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional flag names. Supported: IGNORECASE/I, "
                        "MULTILINE/M, DOTALL/S, UNICODE/U, ASCII/A, "
                        "VERBOSE/X."
                    ),
                },
            },
            "required": ["pattern"],
        },
        "output_schema": _output_schema_object(
            {
                "pattern": {"type": "string"},
                "flags_applied": {"type": "array", "items": {"type": "string"}},
                "results": {"type": "array", "items": {"type": "object"}},
                "compile_error": {"type": ["string", "null"]},
            },
            required=["pattern", "results"],
        ),
        "output_examples": [
            {
                "input": {"pattern": r"\d+", "test_string": "abc 123 def 456"},
                "output": {
                    "pattern": r"\d+",
                    "flags_applied": [],
                    "results": [
                        {
                            "test_string": "abc 123 def 456",
                            "matched": True,
                            "match_count": 2,
                            "matches": [
                                {"match": "123", "span": [4, 7], "groups": []},
                                {"match": "456", "span": [12, 15], "groups": []},
                            ],
                        }
                    ],
                    "compile_error": None,
                },
            }
        ],
    }


def _jwt_validator_spec() -> dict[str, Any]:
    return {
        "agent_id": _JWT_VALIDATOR_AGENT_ID,
        "name": "JWT Validator",
        "description": (
            "Use when the user wants to decode a JWT and optionally verify "
            "its signature. Decodes header + payload, checks exp/nbf/iat "
            "claims, and verifies the signature against either a shared "
            "secret (HS*) or a JWKS endpoint (RS*/ES*). Refuses the "
            "``alg: none`` pitfall."
        ),
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_JWT_VALIDATOR_AGENT_ID],
        "price_per_call_usd": 0.01,
        "tags": ["jwt", "auth", "security", "developer-tools"],
        "match_keywords": [
            "jwt", "decode jwt", "json web token", "verify jwt",
            "jwt validator", "jwks", "jwt signature",
        ],
        "block_keywords": ["audit", "credit card", "joke"],
        "kind": "aztea_built",
        "category": "Security",
        "input_schema": {
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "JWT to decode/verify (3 dot-separated segments).",
                },
                "secret": {
                    "type": "string",
                    "description": "Optional HMAC secret for HS256/384/512 verification.",
                },
                "jwks_url": {
                    "type": "string",
                    "description": "Optional JWKS URL for RS*/ES* verification.",
                    "format": "uri",
                },
                "algorithms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Allow-list of algorithms (e.g. ['HS256','RS256']). "
                        "Defaults to a safe set; 'none' is always rejected."
                    ),
                },
            },
            "required": ["token"],
        },
        "output_schema": _output_schema_object(
            {
                "header": {"type": ["object", "null"]},
                "payload": {"type": ["object", "null"]},
                "signature_valid": {"type": ["boolean", "null"]},
                "verified_with": {"type": "string"},
                "exp_valid": {"type": ["boolean", "null"]},
                "nbf_valid": {"type": ["boolean", "null"]},
                "iat_valid": {"type": ["boolean", "null"]},
                "errors": {"type": "array", "items": {"type": "string"}},
            },
            required=["header", "payload", "verified_with"],
        ),
        "output_examples": [
            {
                "input": {"token": "eyJhbGciOi...short.example.token"},
                "output": {
                    "header": {"alg": "HS256", "typ": "JWT"},
                    "payload": {"sub": "user123", "exp": 9999999999},
                    "signature_valid": None,
                    "verified_with": "none",
                    "exp_valid": True,
                    "nbf_valid": None,
                    "iat_valid": None,
                    "errors": [],
                },
            }
        ],
    }


def _sbom_generator_spec() -> dict[str, Any]:
    return {
        "agent_id": _SBOM_GENERATOR_AGENT_ID,
        "name": "SBOM Generator",
        "description": (
            "Use when the user wants a Software Bill of Materials from a "
            "manifest. Parses requirements.txt, package.json, or "
            "Cargo.toml and emits a CycloneDX 1.5 JSON SBOM with Package "
            "URLs. Direct dependencies only — does not resolve "
            "transitive trees."
        ),
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SBOM_GENERATOR_AGENT_ID],
        "price_per_call_usd": 0.01,
        "tags": ["sbom", "cyclonedx", "supply-chain", "security"],
        "match_keywords": [
            "sbom", "software bill of materials", "cyclonedx",
            "generate sbom", "supply chain inventory",
        ],
        "block_keywords": ["audit", "jwt", "joke"],
        "kind": "aztea_built",
        "category": "Security",
        "input_schema": {
            "type": "object",
            "properties": {
                "manifest_content": {
                    "type": "string",
                    "description": "Contents of the manifest file.",
                },
                "manifest_type": {
                    "type": "string",
                    "enum": ["requirements.txt", "package.json", "Cargo.toml"],
                    "description": "Manifest format.",
                },
                "include_license": {
                    "type": "boolean",
                    "default": True,
                    "description": "Reserved for future SPDX enrichment.",
                },
            },
            "required": ["manifest_content", "manifest_type"],
        },
        "output_schema": _output_schema_object(
            {
                "bom_format": {"type": "string"},
                "spec_version": {"type": "string"},
                "version": {"type": "integer"},
                "metadata": {"type": "object"},
                "components": {"type": "array", "items": {"type": "object"}},
                "component_count": {"type": "integer"},
                "parse_warnings": {"type": "array", "items": {"type": "object"}},
                "manifest_type": {"type": "string"},
            },
            required=["bom_format", "spec_version", "components", "component_count"],
        ),
        "output_examples": [
            {
                "input": {
                    "manifest_content": "requests==2.28.0\nflask>=2.0",
                    "manifest_type": "requirements.txt",
                },
                "output": {
                    "bom_format": "CycloneDX",
                    "spec_version": "1.5",
                    "version": 1,
                    "metadata": {
                        "timestamp": "2026-05-18T00:00:00+00:00",
                        "tools": [
                            {"vendor": "Aztea", "name": "aztea-sbom", "version": "1.0"},
                        ],
                        "manifest_type": "requirements.txt",
                    },
                    "components": [
                        {
                            "type": "library",
                            "name": "requests",
                            "version": "2.28.0",
                            "purl": "pkg:pypi/requests@2.28.0",
                            "bom-ref": "pkg:pypi/requests@2.28.0",
                            "license": None,
                        },
                        {
                            "type": "library",
                            "name": "flask",
                            "version": "2.0",
                            "purl": "pkg:pypi/flask@2.0",
                            "bom-ref": "pkg:pypi/flask@2.0",
                            "license": None,
                        },
                    ],
                    "component_count": 2,
                    "parse_warnings": [],
                    "manifest_type": "requirements.txt",
                },
            }
        ],
    }


def _pypi_metadata_spec() -> dict[str, Any]:
    return {
        "agent_id": _PYPI_METADATA_AGENT_ID,
        "name": "PyPI Metadata",
        "description": (
            "Use when the user wants live PyPI metadata for a Python "
            "package in one round-trip: latest_version, license (with "
            "Trove-classifier fallback), maintainers, release date, "
            "project URLs, requires_python. Returns ``not_found: true`` "
            "for packages absent from PyPI rather than reporting empty "
            "metadata."
        ),
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_PYPI_METADATA_AGENT_ID],
        "price_per_call_usd": 0.01,
        "tags": ["pypi", "python", "packages", "metadata"],
        "match_keywords": [
            "pypi", "pypi metadata", "package metadata",
            "latest version of", "pypi info", "python package info",
            "release date of", "python package license",
        ],
        "block_keywords": ["jwt", "joke"],
        "kind": "aztea_built",
        "category": "Developer Tools",
        "input_schema": {
            "type": "object",
            "properties": {
                "package_name": {
                    "type": "string",
                    "description": "Distribution name on PyPI (e.g. 'pydantic').",
                },
                "version": {
                    "type": "string",
                    "description": "Optional version to pin lookup to; latest if omitted.",
                },
            },
            "required": ["package_name"],
        },
        "output_schema": _output_schema_object(
            {
                "name": {"type": "string"},
                "version": {"type": ["string", "null"]},
                "latest_version": {"type": ["string", "null"]},
                "summary": {"type": ["string", "null"]},
                "license": {"type": ["string", "null"]},
                "classifiers": {"type": "array", "items": {"type": "string"}},
                "maintainers": {"type": "array", "items": {"type": "string"}},
                "requires_python": {"type": ["string", "null"]},
                "release_date": {"type": ["string", "null"]},
                "project_urls": {"type": ["object", "null"]},
                "homepage": {"type": ["string", "null"]},
                "not_found": {"type": "boolean"},
            },
            required=["name", "not_found"],
        ),
        "output_examples": [
            {
                "input": {"package_name": "pydantic"},
                "output": {
                    "name": "pydantic",
                    "version": "2.5.0",
                    "latest_version": "2.5.0",
                    "summary": "Data validation using Python type hints",
                    "license": "MIT License",
                    "classifiers": ["License :: OSI Approved :: MIT License"],
                    "maintainers": ["Samuel Colvin"],
                    "requires_python": ">=3.8",
                    "release_date": "2024-01-15T12:00:00Z",
                    "project_urls": {"Homepage": "https://pydantic.dev"},
                    "homepage": "https://pydantic.dev",
                    "not_found": False,
                },
            }
        ],
    }


def _github_releases_spec() -> dict[str, Any]:
    return {
        "agent_id": _GITHUB_RELEASES_AGENT_ID,
        "name": "GitHub Releases",
        "description": (
            "Use when the user wants the recent releases / changelog of "
            "a GitHub repo. Calls the GitHub Releases REST API (60/h "
            "unauthenticated, 5000/h with GITHUB_TOKEN) and returns "
            "tag, date, prerelease flag, release body (markdown, "
            "truncated), and asset list."
        ),
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_GITHUB_RELEASES_AGENT_ID],
        "price_per_call_usd": 0.01,
        "tags": ["github", "releases", "changelog", "developer-tools"],
        "match_keywords": [
            "github releases", "releases for", "latest release of",
            "recent releases", "changelog of", "github tags",
        ],
        "block_keywords": ["jwt", "joke", "credit card"],
        "kind": "aztea_built",
        "category": "Developer Tools",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "owner/repo (e.g. 'anthropics/anthropic-sdk-python').",
                    "pattern": r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "default": 5,
                    "description": "Max releases to return.",
                },
                "since_version": {
                    "type": "string",
                    "description": (
                        "Filter to releases newer than this tag (coarse "
                        "numeric comparison)."
                    ),
                },
            },
            "required": ["repo"],
        },
        "output_schema": _output_schema_object(
            {
                "repo": {"type": "string"},
                "release_count": {"type": "integer"},
                "releases": {"type": "array", "items": {"type": "object"}},
                "latest_tag": {"type": ["string", "null"]},
                "rate_limit_remaining": {"type": ["integer", "null"]},
            },
            required=["repo", "release_count", "releases"],
        ),
        "output_examples": [
            {
                "input": {"repo": "anthropics/anthropic-sdk-python", "limit": 1},
                "output": {
                    "repo": "anthropics/anthropic-sdk-python",
                    "release_count": 1,
                    "releases": [
                        {
                            "tag_name": "v0.30.0",
                            "name": "v0.30.0",
                            "published_at": "2026-05-01T00:00:00Z",
                            "is_prerelease": False,
                            "is_draft": False,
                            "body": "Added streaming support.",
                            "html_url": "https://github.com/anthropics/anthropic-sdk-python/releases/tag/v0.30.0",
                            "asset_count": 0,
                            "assets": [],
                        }
                    ],
                    "latest_tag": "v0.30.0",
                    "rate_limit_remaining": 59,
                },
            }
        ],
    }


def _hcl_terraform_analyzer_spec() -> dict[str, Any]:
    return {
        "agent_id": _HCL_TERRAFORM_ANALYZER_AGENT_ID,
        "name": "HCL / Terraform Analyzer",
        "description": (
            "Use when the user wants security scanning of raw Terraform "
            "HCL (not ``terraform plan`` output). Runs checkov in a "
            "sandbox tempdir and returns CIS/PCI/HIPAA findings with "
            "check ids, severities, resource paths, file line ranges, "
            "and guideline URLs. Distinct from terraform_plan_analyzer, "
            "which consumes ``terraform plan -json`` output."
        ),
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_HCL_TERRAFORM_ANALYZER_AGENT_ID],
        "price_per_call_usd": 0.02,
        "tags": ["terraform", "hcl", "iac", "security", "checkov"],
        "match_keywords": [
            "terraform", "hcl", "tfsec", "checkov", "kics",
            "terraform security", "iac security", "scan terraform",
            "terraform lint", "terraform misconfiguration",
        ],
        "block_keywords": ["plan json", "terraform plan output", "jwt", "joke"],
        "kind": "aztea_built",
        "category": "Security",
        "examples_sensitive": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "hcl_content": {
                    "type": "string",
                    "description": "Raw Terraform HCL source.",
                },
                "frameworks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional compliance subset (e.g. ['CIS','PCI']).",
                },
                "skip_checks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional checkov check IDs to skip.",
                },
            },
            "required": ["hcl_content"],
        },
        "output_schema": _output_schema_object(
            {
                "tool": {"type": "string"},
                "tool_version": {"type": ["string", "null"]},
                "passed_count": {"type": "integer"},
                "failed_count": {"type": "integer"},
                "findings": {"type": "array", "items": {"type": "object"}},
                "severity_counts": {"type": "object"},
                "summary": {"type": "string"},
            },
            required=["tool", "passed_count", "failed_count", "findings", "summary"],
        ),
        "output_examples": [
            {
                "input": {
                    "hcl_content": (
                        'resource "aws_s3_bucket" "example" {\n'
                        '  bucket = "my-bucket"\n'
                        '}\n'
                    ),
                },
                "output": {
                    "tool": "checkov",
                    "tool_version": "2.5.0",
                    "passed_count": 1,
                    "failed_count": 3,
                    "findings": [
                        {
                            "check_id": "CKV_AWS_18",
                            "check_name": "Ensure the S3 bucket has access logging enabled",
                            "severity": "medium",
                            "resource": "aws_s3_bucket.example",
                            "file_line_range": [1, 3],
                            "guideline": "https://docs.bridgecrew.io/...",
                        },
                    ],
                    "severity_counts": {"critical": 0, "high": 1, "medium": 2, "low": 0},
                    "summary": "Scanned with checkov: 3 failed / 1 passed.",
                },
            }
        ],
    }


def load_builtin_specs_part3() -> list[dict[str, Any]]:
    return [
        _regex_tester_spec(),
        _jwt_validator_spec(),
        _sbom_generator_spec(),
        _pypi_metadata_spec(),
        _github_releases_spec(),
        _hcl_terraform_analyzer_spec(),
    ]
