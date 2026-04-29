"""Second chunk of built-in agent specs (argument to `specs.extend([...])`)."""
from __future__ import annotations

from typing import Any

from server.builtin_agents import pricing_overlay as _pricing_overlay
from server.builtin_agents.constants import (
    ARXIV_RESEARCH_AGENT_ID as _ARXIV_RESEARCH_AGENT_ID,
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
    DEPENDENCY_AUDITOR_AGENT_ID as _DEPENDENCY_AUDITOR_AGENT_ID,
    DNS_INSPECTOR_AGENT_ID as _DNS_INSPECTOR_AGENT_ID,
    HN_DIGEST_AGENT_ID as _HN_DIGEST_AGENT_ID,
    IMAGE_GENERATOR_AGENT_ID as _IMAGE_GENERATOR_AGENT_ID,
    PYTHON_EXECUTOR_AGENT_ID as _PYTHON_EXECUTOR_AGENT_ID,
    VIDEO_STORYBOARD_AGENT_ID as _VIDEO_STORYBOARD_AGENT_ID,
    WEB_RESEARCHER_AGENT_ID as _WEB_RESEARCHER_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object


def load_builtin_specs_part2() -> list[dict[str, Any]]:
    return [
    {
        "agent_id": _IMAGE_GENERATOR_AGENT_ID,
        "name": "Image Generator Agent",
        "description": "Use when the task requires generating an image. Runs a real model backend (OpenAI gpt-image-1 or Replicate) — not a description. Returns base64-encoded image artifacts, supports reference images for style guidance.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_IMAGE_GENERATOR_AGENT_ID],
        "price_per_call_usd": 0.06,
        "tags": ["image-generation", "creative", "multimodal", "design"],
        "input_schema": _output_schema_object(
            {
                "prompt": {
                    "type": "string",
                    "title": "Prompt",
                    "description": "What should the image show? Describe subject, style, lighting, and composition.",
                    "maxLength": 1200,
                    "examples": ["A retro-futurist skyline at sunrise, cinematic lighting"],
                },
                "style": {
                    "type": "string",
                    "title": "Style",
                    "description": "Optional style keyword (e.g. photorealistic, synthwave, watercolour).",
                    "examples": ["synthwave", "photorealistic", "flat vector"],
                },
                "width": {
                    "type": "integer",
                    "title": "Width (px)",
                    "description": "Output width in pixels. Larger sizes cost more.",
                    "default": 1024,
                    "minimum": 256,
                    "maximum": 2048,
                },
                "height": {
                    "type": "integer",
                    "title": "Height (px)",
                    "description": "Output height in pixels. Larger sizes cost more.",
                    "default": 1024,
                    "minimum": 256,
                    "maximum": 2048,
                },
                "image_count": {
                    "type": "integer",
                    "title": "Number of images",
                    "description": "How many images to render. Billed per image.",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 6,
                },
                "high_res": {
                    "type": "boolean",
                    "title": "High-resolution render",
                    "description": "Enable the 2x high-resolution multiplier. Charge doubles.",
                    "default": False,
                },
                "output_format": {
                    "type": "string",
                    "title": "File format",
                    "description": "Preferred output container.",
                    "enum": ["png", "jpeg", "webp"],
                    "default": "png",
                },
                "input_images": {
                    "type": "array",
                    "title": "Reference images",
                    "description": "Optional reference images to guide the render.",
                    "items": {
                        "type": "object",
                        "title": "Reference image",
                        "description": "Either a public https URL or a data-URL.",
                        "properties": {
                            "mime": {"type": "string", "description": "e.g. image/png"},
                            "url_or_base64": {
                                "type": "string",
                                "description": "https URL or data:image/*;base64,... payload",
                                "format": "uri",
                            },
                        },
                    },
                },
            },
            required=["prompt"],
        ),
        "output_schema": _output_schema_object(
            {
                "summary": {"type": "string"},
                "generation_prompt": {"type": "string"},
                "artifacts": {"type": "array", "items": {"type": "object"}},
                "input_images_used": {"type": "integer"},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "billing_units_actual": {"type": "integer", "description": "Actual number of images produced; used for variable-pricing refunds."},
            },
            required=["summary", "artifacts"],
        ),
        "output_examples": [
            {
                "input": {
                    "prompt": "A retro-futurist skyline at sunrise, cinematic lighting",
                    "style": "synthwave",
                    "width": 1024,
                    "height": 1024,
                    "image_count": 1,
                    "high_res": False,
                },
                "output": {
                    "summary": "Generated one image artifact using a live model backend.",
                    "generation_prompt": "A retro-futurist skyline at sunrise, cinematic lighting",
                    "artifacts": [
                        {
                            "name": "generated.png",
                            "mime": "image/png",
                            "url_or_base64": "data:image/png;base64,...",
                            "size_bytes": 245801,
                        }
                    ],
                    "input_images_used": 0,
                    "warnings": [],
                },
            }
        ],
    },
    {
        "agent_id": _VIDEO_STORYBOARD_AGENT_ID,
        "name": "Video Storyboard Generator Agent",
        "description": "Use when the task requires generating a short video from a creative brief. Produces a shot plan, voiceover script, and an actual video artifact via a configured model backend — not a storyboard description.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_VIDEO_STORYBOARD_AGENT_ID],
        "price_per_call_usd": 0.22,
        "tags": ["video-generation", "storyboarding", "multimodal", "creative"],
        "input_schema": _output_schema_object(
            {
                "brief": {
                    "type": "string",
                    "title": "Creative brief",
                    "description": "Describe the story, target audience, and desired mood.",
                    "contentMediaType": "text/markdown",
                    "maxLength": 4000,
                    "examples": ["Launch teaser for an AI healthcare assistant for busy parents."],
                },
                "duration_seconds": {
                    "type": "integer",
                    "title": "Duration (seconds)",
                    "description": "Target video length. Billed per second produced.",
                    "default": 8,
                    "minimum": 3,
                    "maximum": 20,
                },
                "aspect_ratio": {
                    "type": "string",
                    "title": "Aspect ratio",
                    "description": "Frame aspect ratio for the render.",
                    "enum": ["16:9", "9:16", "1:1", "4:5", "21:9"],
                    "default": "16:9",
                },
                "style": {
                    "type": "string",
                    "title": "Visual style",
                    "description": "Free-form style keyword (e.g. cinematic, documentary, anime).",
                    "examples": ["clean cinematic", "documentary", "vintage 8mm"],
                },
                "reference_images": {
                    "type": "array",
                    "title": "Reference images",
                    "description": "Optional moodboard references for style transfer.",
                    "items": {
                        "type": "object",
                        "title": "Reference image",
                        "description": "Public https URL or data-URL payload.",
                        "properties": {
                            "mime": {"type": "string"},
                            "url_or_base64": {"type": "string", "format": "uri"},
                        },
                    },
                },
            },
            required=["brief"],
        ),
        "output_schema": _output_schema_object(
            {
                "title": {"type": "string"},
                "duration_seconds": {"type": "integer"},
                "aspect_ratio": {"type": "string"},
                "style": {"type": "string"},
                "shot_plan": {"type": "array", "items": {"type": "object"}},
                "voiceover_script": {"type": "string"},
                "render_recipe": {"type": "object"},
                "artifacts": {"type": "array", "items": {"type": "object"}},
                "billing_units_actual": {"type": "integer", "description": "Actual seconds of video rendered; used for variable-pricing refunds."},
            },
            required=["title", "shot_plan", "voiceover_script", "artifacts"],
        ),
        "output_examples": [
            {
                "input": {
                    "brief": "Launch teaser for an AI healthcare assistant for busy parents.",
                    "duration_seconds": 30,
                    "aspect_ratio": "16:9",
                    "style": "clean cinematic",
                },
                "output": {
                    "title": "Launch teaser: AI healthcare assistant for busy parents",
                    "duration_seconds": 30,
                    "aspect_ratio": "16:9",
                    "style": "clean cinematic",
                    "shot_plan": [
                        {
                            "shot_id": 1,
                            "start_second": 0,
                            "end_second": 6,
                            "visual_prompt": "Morning rush at home, parent checking phone for care guidance",
                        }
                    ],
                    "voiceover_script": "When every minute matters, trusted care guidance should be one tap away.",
                    "render_recipe": {"target_fps": 24, "color_profile": "rec709", "provider": "replicate"},
                    "artifacts": [
                        {
                            "name": "generated.mp4",
                            "mime": "video/mp4",
                            "url_or_base64": "https://cdn.example.com/generated.mp4",
                            "size_bytes": 0,
                        }
                    ],
                },
            }
        ],
    },
{
    "agent_id": _ARXIV_RESEARCH_AGENT_ID,
    "name": "arXiv Research Agent",
    "description": "Use when the user wants live academic papers from arXiv, not LLM-recalled citations. Searches arXiv.org by keyword, author, or category and returns real papers with abstracts, a synthesis of key themes, seminal works, and open research questions.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_ARXIV_RESEARCH_AGENT_ID],
    "price_per_call_usd": 0.06,
    "tags": ["research", "academic", "arxiv", "papers", "science"],
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (keywords, author name, topic)", "example": "diffusion models image generation"},
            "max_results": {"type": "integer", "default": 8, "minimum": 1, "maximum": 20, "description": "Number of papers to fetch"},
            "sort_by": {"type": "string", "enum": ["relevance", "lastUpdatedDate", "submittedDate"], "default": "relevance"},
            "categories": {"type": "array", "items": {"type": "string"}, "description": "arXiv category filters e.g. cs.AI, stat.ML", "example": ["cs.LG", "cs.AI"]},
            "full_abstract": {"type": "boolean", "default": False, "description": "Fetch full abstract page for top 3 papers to extract affiliations and related links"},
        },
        "required": ["query"],
    },
    "output_schema": _output_schema_object(
        {
            "query": {"type": "string"},
            "total_found": {"type": "integer"},
            "papers": {"type": "array", "items": {"type": "object"}},
            "synthesis": {"type": "string"},
            "key_themes": {"type": "array", "items": {"type": "string"}},
            "seminal_papers": {"type": "array", "items": {"type": "string"}},
            "open_questions": {"type": "array", "items": {"type": "string"}},
            "suggested_follow_ups": {"type": "array", "items": {"type": "string"}},
            "billing_units_actual": {"type": "integer", "description": "Actual number of papers returned; used for variable-pricing refunds."},
        },
        required=["query", "papers", "synthesis"],
    ),
    "variable_pricing": {
        "model": "per_unit",
        "field": "max_results",
        "field_type": "scalar",
        "unit_label": "paper",
        "rate_usd": 0.03,
        "min_usd": 0.05,
    },
    "output_examples": [
        {
            "input": {"query": "transformer attention self-supervised", "max_results": 5},
            "output": {
                "query": "transformer attention self-supervised",
                "total_found": 5,
                "papers": [
                    {
                        "arxiv_id": "2010.11929",
                        "title": "An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale",
                        "authors": ["Alexey Dosovitskiy", "Lucas Beyer"],
                        "abstract": "While the Transformer architecture has become the de-facto standard for NLP tasks, its applications to computer vision remain limited...",
                        "categories": ["cs.CV"],
                        "published": "2020-10-22",
                        "updated": "2021-06-03",
                        "pdf_url": "https://arxiv.org/pdf/2010.11929",
                        "abstract_url": "https://arxiv.org/abs/2010.11929",
                    }
                ],
                "synthesis": "The literature shows a clear convergence on attention mechanisms replacing convolutional backbones for vision tasks, with self-supervised pre-training bridging the gap between label-efficient and high-performance models.",
                "key_themes": ["vision transformers", "self-supervised pre-training", "attention at scale"],
                "seminal_papers": ["2010.11929"],
                "open_questions": ["Can attention fully replace inductive biases from CNNs?", "Scaling limits of self-supervised vision models"],
                "suggested_follow_ups": ["masked autoencoders MAE", "CLIP vision language pretraining"],
            },
        }
    ],
},
{
    "agent_id": _PYTHON_EXECUTOR_AGENT_ID,
    "name": "Python Code Executor",
    "description": "Use when the user wants to actually run Python code, not simulate it. Executes in a real sandboxed subprocess. Returns stdout, stderr, exit code, execution time, and an expert explanation of what the output means.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_PYTHON_EXECUTOR_AGENT_ID],
    "price_per_call_usd": 0.06,
    "tags": ["code-execution", "python", "developer-tools", "compute"],
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to execute", "example": "print(sum(i**2 for i in range(10)))"},
            "stdin": {"type": "string", "default": "", "description": "Optional input data fed to stdin"},
            "timeout": {"type": "integer", "default": 10, "minimum": 1, "maximum": 30, "description": "Execution timeout in seconds"},
            "explain": {"type": "boolean", "default": True, "description": "Whether to include an expert explanation of the output"},
        },
        "required": ["code"],
    },
    "output_schema": _output_schema_object(
        {
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            "exit_code": {"type": "integer"},
            "timed_out": {"type": "boolean"},
            "execution_time_ms": {"type": "integer"},
            "explanation": {"type": "string"},
            "variables_captured": {"type": "object"},
        },
        required=["stdout", "exit_code"],
    ),
    "variable_pricing": {
        "model": "per_unit",
        "field": "timeout",
        "field_type": "scalar",
        "unit_label": "second",
        "rate_usd": 0.02,
        "min_usd": 0.05,
    },
    "output_examples": [
        {
            "input": {"code": "import math\nresult = [math.factorial(n) for n in range(1, 11)]\nprint(result)", "explain": True},
            "output": {
                "stdout": "[1, 2, 6, 24, 120, 720, 5040, 40320, 362880, 3628800]\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "execution_time_ms": 28,
                "explanation": "The code computes factorials 1! through 10! using a list comprehension over math.factorial. Output is correct — factorials grow rapidly and 10! = 3,628,800 as expected.",
                "variables_captured": {"result": [1, 2, 6, 24, 120, 720, 5040, 40320, 362880, 3628800]},
            },
        }
    ],
},
{
    "agent_id": _WEB_RESEARCHER_AGENT_ID,
    "name": "Web Researcher Agent",
    "description": "Use when the task requires reading a live URL, not guessing its content. Fetches the page in real time and returns a dense summary, key points, direct answers to a specific question, and verbatim supporting quotes. Supports up to 10 URLs in one call for cross-source synthesis.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_WEB_RESEARCHER_AGENT_ID],
    "price_per_call_usd": 0.03,
    "tags": ["web", "research", "summarization", "content-extraction"],
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Public URL to fetch and analyze", "example": "https://en.wikipedia.org/wiki/Large_language_model"},
            "urls": {"type": "array", "items": {"type": "string"}, "description": "Multiple URLs to fetch and cross-reference (max 10)"},
            "question": {"type": "string", "default": "", "description": "Specific question to answer from the page content"},
            "mode": {"type": "string", "enum": ["summary", "extract", "qa"], "default": "summary", "description": "Analysis mode"},
        },
        "required": [],
    },
    "output_schema": _output_schema_object(
        {
            "url": {"type": "string"},
            "urls": {"type": "array", "items": {"type": "string"}},
            "title": {"type": "string"},
            "word_count": {"type": "integer"},
            "fetched_at": {"type": "string"},
            "summary": {"type": "string"},
            "key_points": {"type": "array", "items": {"type": "string"}},
            "answer": {"type": "string"},
            "quotes": {"type": "array", "items": {"type": "string"}},
            "links": {"type": "array", "items": {"type": "object"}},
            "content_type": {"type": "string"},
            "per_url_findings": {"type": "array", "items": {"type": "object"}, "description": "Per-URL fetch status and content_length (no raw content)"},
            "synthesis": {"type": "string", "description": "LLM synthesis across all fetched content"},
            "cross_source_consensus": {"type": ["string", "null"], "description": "Single sentence on what all sources agree on (null for single-URL calls)"},
            "billing_units_actual": {"type": "integer", "description": "Number of URLs successfully fetched; used for variable-pricing refunds."},
        },
        required=["per_url_findings", "billing_units_actual"],
    ),
    "variable_pricing": {
        "model": "tiered",
        "field": "urls",
        "field_type": "array",
        "unit_label": "URL",
        "tiers": [
            {"max_units": 1,  "price_usd": 0.02},
            {"max_units": 3,  "price_usd": 0.05},
            {"max_units": 6,  "price_usd": 0.09},
            {"max_units": 10, "price_usd": 0.15},
        ],
    },
    "output_examples": [
        {
            "input": {"url": "https://en.wikipedia.org/wiki/Transformer_(machine_learning_model)", "question": "What is the key innovation of transformers?"},
            "output": {
                "url": "https://en.wikipedia.org/wiki/Transformer_(machine_learning_model)",
                "title": "Transformer (machine learning model)",
                "word_count": 4200,
                "fetched_at": "2026-01-15T10:30:00+00:00",
                "summary": "The Transformer is a deep learning architecture introduced in the 2017 paper 'Attention Is All You Need'. It relies entirely on self-attention mechanisms, eliminating recurrence and enabling massive parallelization during training.",
                "key_points": [
                    "Introduced by Vaswani et al. (2017) at Google Brain",
                    "Self-attention enables O(1) path length between any two tokens",
                    "Forms the basis of GPT, BERT, and most modern LLMs",
                ],
                "answer": "The key innovation is replacing recurrence entirely with self-attention, which allows all token positions to attend to each other simultaneously, enabling parallelization and capturing long-range dependencies more effectively.",
                "quotes": ["Attention is All You Need", "The model architecture avoids recurrence"],
                "links": [{"text": "Attention Is All You Need", "href": "https://arxiv.org/abs/1706.03762"}],
                "content_type": "article",
            },
        }
    ],
},
{
    "agent_id": str(_DNS_INSPECTOR_AGENT_ID),
    "name": "DNS & SSL Inspector",
    "description": "Use when the task requires checking domain health: DNS records, SSL certificate expiry, or HTTP security headers. Runs live checks against up to 10 domains and returns structured findings with actionable issues.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_DNS_INSPECTOR_AGENT_ID],
    "price_per_call_usd": 0.09,
    "tags": ["dns", "ssl", "security", "infrastructure"],
    "kind": "aztea_built",
    "category": "Security",
    "is_featured": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "domains": {"type": "array", "items": {"type": "string"}, "description": "Domain names to inspect (max 10)"},
            "checks": {"type": "array", "items": {"type": "string"}, "default": ["dns", "ssl", "http"], "description": "Checks to run: dns, ssl, http, mx"},
        },
        "required": ["domains"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "results": {"type": "array"},
            "billing_units_actual": {"type": "integer"},
        },
    },
    "variable_pricing": {
        "model": "tiered",
        "field": "domains",
        "field_type": "array",
        "unit_label": "domain",
        "tiers": [
            {"max_units": 1,  "price_usd": 0.04},
            {"max_units": 3,  "price_usd": 0.09},
            {"max_units": 10, "price_usd": 0.16},
        ],
    },
    "output_examples": [
        {
            "input": {"domains": ["example.com"], "checks": ["dns", "ssl"]},
            "output": {
                "results": [
                    {
                        "domain": "example.com",
                        "dns": {"a": ["93.184.216.34"], "mx": [], "ns": ["a.iana-servers.net"]},
                        "ssl": {"valid": True, "expires_in_days": 180, "issuer": "DigiCert Inc", "subject": "example.com"},
                        "issues": [],
                    }
                ],
                "billing_units_actual": 1,
            },
        }
    ],
},
    # ── Dependency Auditor ───────────────────────────────────────────────────
    {
        "agent_id": _DEPENDENCY_AUDITOR_AGENT_ID,
        "name": "Dependency Auditor",
        "description": "Use this when the user wants to audit their dependencies for security vulnerabilities, outdated packages, or license issues. Accepts the text of a package.json or requirements.txt and returns a structured report: CVEs per package, license risks, and prioritized upgrade recommendations.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_DEPENDENCY_AUDITOR_AGENT_ID],
        "price_per_call_usd": 0.04,
        "tags": ["security", "cve", "dependencies", "npm", "pypi", "developer-tools"],
        "kind": "aztea_built",
        "category": "Data",
        "is_featured": True,
        "input_schema": _output_schema_object(
            {
                "manifest": {
                    "type": "string",
                    "title": "Package Manifest",
                    "description": "Contents of package.json or requirements.txt (paste the full file).",
                    "maxLength": 10000,
                },
                "ecosystem": {
                    "type": "string",
                    "title": "Ecosystem",
                    "description": "Package ecosystem. 'auto' detects from manifest format.",
                    "default": "auto",
                    "enum": ["npm", "pypi", "auto"],
                },
                "checks": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["cve", "outdated", "license"]},
                    "title": "Checks",
                    "description": "Which checks to run. Defaults to all three.",
                    "default": ["cve", "outdated", "license"],
                },
            },
            required=["manifest"],
        ),
        "output_schema": _output_schema_object(
            {
                "ecosystem": {"type": "string"},
                "total_packages": {"type": "integer"},
                "vulnerable_count": {"type": "integer"},
                "outdated_count": {"type": "integer"},
                "critical_count": {"type": "integer"},
                "packages": {"type": "array", "items": {"type": "object"}},
                "top_priorities": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
            },
            required=["ecosystem", "total_packages", "packages", "summary"],
        ),
        "output_examples": [
            {
                "input": {"manifest": '{"dependencies": {"lodash": "4.17.20"}}', "ecosystem": "npm"},
                "output": {
                    "ecosystem": "npm",
                    "total_packages": 1,
                    "vulnerable_count": 1,
                    "outdated_count": 1,
                    "critical_count": 1,
                    "packages": [
                        {
                            "name": "lodash",
                            "current_version": "4.17.20",
                            "latest_version": "4.17.21",
                            "cves": [{"id": "CVE-2021-23337", "severity": "high", "description": "Command injection via template", "fixed_in": "4.17.21"}],
                            "license": "MIT",
                            "license_risk": "none",
                            "action": "upgrade",
                        }
                    ],
                    "top_priorities": ["Upgrade lodash to 4.17.21 (CVE-2021-23337)"],
                    "summary": "1 package with a high-severity CVE. Upgrade lodash immediately.",
                },
            }
        ],
    },
    ]


def _assert_prices_match_overlay(specs: list[dict[str, Any]]) -> None:
    """Raise at import time if a spec's price_per_call_usd drifts below its overlay minimum.

    The overlay is the canonical pricing source for variable-priced agents.
    A spec price BELOW the overlay minimum means callers see an inaccurate
    price in discovery — catch this at startup, not at runtime.
    """
    overlay = _pricing_overlay.get_pricing_overlay()
    for spec in specs:
        agent_id = spec.get("agent_id", "")
        if agent_id not in overlay:
            continue
        config = overlay[agent_id].get("pricing_config", {})
        min_cents = config.get("min_cents", 0)
        min_usd = min_cents / 100
        spec_price = float(spec.get("price_per_call_usd", 0))
        if spec_price < min_usd - 0.0001:  # 0.1-cent tolerance for float arithmetic
            raise RuntimeError(
                f"Price drift detected for agent {agent_id!r}: "
                f"spec price ${spec_price:.4f} < overlay minimum ${min_usd:.4f}. "
                "Update pricing_overlay.py or the spec to match."
            )


# Eagerly validate on import so startup fails fast on price drift.
_assert_prices_match_overlay(load_builtin_specs_part2())
