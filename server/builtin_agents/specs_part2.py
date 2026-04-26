"""Second chunk of built-in agent specs (argument to `specs.extend([...])`)."""
from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    ARXIV_RESEARCH_AGENT_ID as _ARXIV_RESEARCH_AGENT_ID,
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
    DEPENDENCY_AUDITOR_AGENT_ID as _DEPENDENCY_AUDITOR_AGENT_ID,
    DNS_INSPECTOR_AGENT_ID as _DNS_INSPECTOR_AGENT_ID,
    GITHUB_FETCHER_AGENT_ID as _GITHUB_FETCHER_AGENT_ID,
    HN_DIGEST_AGENT_ID as _HN_DIGEST_AGENT_ID,
    IMAGE_GENERATOR_AGENT_ID as _IMAGE_GENERATOR_AGENT_ID,
    PR_REVIEWER_AGENT_ID as _PR_REVIEWER_AGENT_ID,
    PYTHON_EXECUTOR_AGENT_ID as _PYTHON_EXECUTOR_AGENT_ID,
    SPEC_WRITER_AGENT_ID as _SPEC_WRITER_AGENT_ID,
    TEST_GENERATOR_AGENT_ID as _TEST_GENERATOR_AGENT_ID,
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
        "price_per_call_usd": 0.02,
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
        "price_per_call_usd": 0.03,
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
    "price_per_call_usd": 0.01,
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
    "price_per_call_usd": 0.01,
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
    "price_per_call_usd": 0.01,
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
        required=["synthesis", "per_url_findings", "billing_units_actual"],
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
    "agent_id": str(_GITHUB_FETCHER_AGENT_ID),
    "name": "GitHub File Fetcher",
    "description": "Use when the task requires reading files from a public GitHub repository. Fetches up to 20 files in parallel, auto-detects the default branch, and optionally returns an LLM summary of the codebase architecture. Returns file content verbatim.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_GITHUB_FETCHER_AGENT_ID],
    "price_per_call_usd": 0.08,
    "tags": ["github", "code", "files", "repository"],
    "input_schema": {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Repository in owner/repo format"},
            "paths": {"type": "array", "items": {"type": "string"}, "description": "File paths to fetch (max 20)"},
            "branch": {"type": "string", "default": "main", "description": "Branch name"},
            "summarize": {"type": "boolean", "default": False, "description": "Run LLM summary of fetched files"},
        },
        "required": ["repo", "paths"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "branch": {"type": "string"},
            "files": {"type": "array"},
            "summary": {"type": "string"},
            "billing_units_actual": {"type": "integer"},
        },
    },
    "variable_pricing": {
        "model": "tiered",
        "field": "paths",
        "field_type": "array",
        "unit_label": "file",
        "tiers": [
            {"max_units": 1,  "price_usd": 0.03},
            {"max_units": 5,  "price_usd": 0.08},
            {"max_units": 20, "price_usd": 0.18},
        ],
    },
    "output_examples": [
        {
            "input": {"repo": "openai/openai-python", "paths": ["README.md"], "branch": "main"},
            "output": {
                "repo": "openai/openai-python",
                "branch": "main",
                "files": [{"path": "README.md", "content": "# OpenAI Python SDK\n...", "size_bytes": 4096}],
                "summary": "The openai-python repository contains the official Python client for the OpenAI API.",
                "billing_units_actual": 1,
            },
        }
    ],
},
{
    "agent_id": str(_HN_DIGEST_AGENT_ID),
    "name": "Hacker News Digest",
    "description": "Use when the user wants the current Hacker News front page, not cached results. Fetches live stories via the HN Algolia API and returns a synthesis of themes, trending topics, and notable discussions.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_HN_DIGEST_AGENT_ID],
    "price_per_call_usd": 0.10,
    "tags": ["news", "hacker-news", "trends", "digest"],
    "input_schema": {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "default": 10, "description": "Number of stories (max 20)"},
            "topic_filter": {"type": "string", "description": "Filter stories by keyword in title"},
            "mode": {"type": "string", "enum": ["digest", "trends", "hot"], "default": "digest"},
        },
        "required": [],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "stories": {"type": "array"},
            "synthesis": {"type": "string"},
            "trending_topics": {"type": "array"},
            "notable_discussions": {"type": "array"},
            "billing_units_actual": {"type": "integer"},
        },
    },
    "variable_pricing": {
        "model": "per_unit",
        "field": "count",
        "field_type": "scalar",
        "unit_label": "story",
        "rate_usd": 0.02,
        "min_usd": 0.05,
    },
    "output_examples": [
        {
            "input": {"count": 5, "mode": "digest"},
            "output": {
                "stories": [{"title": "Show HN: Fast SQLite for Python", "score": 342, "url": "https://github.com/example/fast-sqlite"}],
                "synthesis": "Today's HN front page is dominated by open-source developer tooling and AI infrastructure stories.",
                "trending_topics": ["sqlite", "AI", "developer-tools"],
                "notable_discussions": ["Show HN: Fast SQLite for Python (342 points, 87 comments)"],
                "billing_units_actual": 5,
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
    # ── PR Reviewer ──────────────────────────────────────────────────────────
    {
        "agent_id": _PR_REVIEWER_AGENT_ID,
        "name": "PR Reviewer",
        "description": "Use this when the user wants to review a GitHub PR or diff. Fetches the actual diff from GitHub and returns structured findings ranked by severity (blocking/warning/suggestion), each with a file path, line hint, CWE/category, description, and copy-paste fix. Pass a GitHub PR URL or raw unified diff text.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_PR_REVIEWER_AGENT_ID],
        "price_per_call_usd": 0.05,
        "tags": ["code-review", "github", "pull-request", "security", "developer-tools"],
        "kind": "aztea_built",
        "category": "Code",
        "is_featured": True,
        "input_schema": _output_schema_object(
            {
                "pr_url": {
                    "type": "string",
                    "title": "GitHub PR URL",
                    "description": "Public GitHub pull request URL, e.g. https://github.com/owner/repo/pull/123",
                    "examples": ["https://github.com/expressjs/express/pull/5742"],
                },
                "diff": {
                    "type": "string",
                    "title": "Raw Diff",
                    "description": "Unified diff text (alternative to pr_url for private repos or non-GitHub hosts).",
                },
                "context": {
                    "type": "string",
                    "title": "Context",
                    "description": "Optional: what does this repo do? Any coding standards to check against?",
                    "maxLength": 600,
                },
            },
            required=[],
        ),
        "output_schema": _output_schema_object(
            {
                "pr_title": {"type": "string"},
                "total_issues": {"type": "integer"},
                "blocking": {"type": "boolean"},
                "issues": {"type": "array", "items": {"type": "object"}},
                "summary": {"type": "string"},
                "verdict": {"type": "string"},
            },
            required=["total_issues", "blocking", "issues", "summary", "verdict"],
        ),
        "output_examples": [
            {
                "input": {"pr_url": "https://github.com/owner/repo/pull/42"},
                "output": {
                    "pr_title": "PR #42",
                    "total_issues": 2,
                    "blocking": True,
                    "issues": [
                        {
                            "file": "src/auth.js",
                            "line_hint": "const query = `SELECT * FROM users WHERE id = ${req.params.id}`",
                            "severity": "critical",
                            "category": "security",
                            "description": "SQL injection via unsanitized req.params.id",
                            "suggestion": "Use parameterized query: db.query('SELECT * FROM users WHERE id = ?', [req.params.id])",
                        }
                    ],
                    "summary": "One critical SQL injection and one missing input validation. Recommend request_changes.",
                    "verdict": "request_changes",
                },
            }
        ],
    },
    # ── Test Generator ───────────────────────────────────────────────────────
    {
        "agent_id": _TEST_GENERATOR_AGENT_ID,
        "name": "Test Generator",
        "description": "Use this when the user wants tests written for their code. Accepts source code and returns a complete runnable test suite — pytest, Jest, Vitest, JUnit, or Go test depending on the language. Covers happy paths, edge cases, and error conditions. The output can be dropped directly into the project.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_TEST_GENERATOR_AGENT_ID],
        "price_per_call_usd": 0.05,
        "tags": ["testing", "pytest", "jest", "developer-tools", "tdd"],
        "kind": "aztea_built",
        "category": "Code",
        "is_featured": True,
        "input_schema": _output_schema_object(
            {
                "code": {
                    "type": "string",
                    "title": "Source Code",
                    "description": "The code to generate tests for.",
                    "maxLength": 12000,
                },
                "language": {
                    "type": "string",
                    "title": "Language",
                    "description": "Programming language. Use 'auto' for detection.",
                    "default": "auto",
                    "examples": ["python", "javascript", "typescript", "go", "java"],
                },
                "framework": {
                    "type": "string",
                    "title": "Testing Framework",
                    "description": "Test framework to use. Use 'auto' to pick based on language.",
                    "default": "auto",
                    "examples": ["pytest", "jest", "vitest", "go_test", "junit"],
                },
                "style": {
                    "type": "string",
                    "title": "Test Style",
                    "description": "Whether to generate unit tests, integration tests, or both.",
                    "default": "both",
                    "enum": ["unit", "integration", "both"],
                },
                "context": {
                    "type": "string",
                    "title": "Context",
                    "description": "Optional: what does this code do? Any important dependencies?",
                    "maxLength": 500,
                },
            },
            required=["code"],
        ),
        "output_schema": _output_schema_object(
            {
                "language": {"type": "string"},
                "framework": {"type": "string"},
                "test_count": {"type": "integer"},
                "coverage_areas": {"type": "array", "items": {"type": "string"}},
                "test_code": {"type": "string"},
                "setup_notes": {"type": "string"},
                "summary": {"type": "string"},
            },
            required=["language", "framework", "test_count", "test_code", "summary"],
        ),
        "output_examples": [
            {
                "input": {"code": "def add(a, b):\n    return a + b", "language": "python", "framework": "pytest"},
                "output": {
                    "language": "python",
                    "framework": "pytest",
                    "test_count": 5,
                    "coverage_areas": ["basic addition", "negative numbers", "zero", "floats", "type error"],
                    "test_code": "import pytest\nfrom module import add\n\ndef test_add_positive():\n    assert add(1, 2) == 3\n",
                    "setup_notes": "",
                    "summary": "5 tests covering positive numbers, negatives, zero, floats, and type safety.",
                },
            }
        ],
    },
    # ── Spec Writer ──────────────────────────────────────────────────────────
    {
        "agent_id": _SPEC_WRITER_AGENT_ID,
        "name": "Spec Writer",
        "description": "Use this when the user wants to write a spec, PRD, RFC, ADR, or API spec from requirements. Accepts a description of what to build and returns a complete, formatted technical document with sections, open questions, out-of-scope items, and a complexity estimate.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SPEC_WRITER_AGENT_ID],
        "price_per_call_usd": 0.05,
        "tags": ["documentation", "specifications", "prd", "rfc", "developer-tools"],
        "kind": "aztea_built",
        "category": "Code",
        "is_featured": False,
        "input_schema": _output_schema_object(
            {
                "requirements": {
                    "type": "string",
                    "title": "Requirements",
                    "description": "Feature description, user story, or problem statement to turn into a spec.",
                    "maxLength": 8000,
                },
                "format": {
                    "type": "string",
                    "title": "Spec Format",
                    "description": "Output format. 'auto' picks the best fit.",
                    "default": "auto",
                    "enum": ["prd", "rfc", "adr", "api_spec", "auto"],
                },
                "stack": {
                    "type": "string",
                    "title": "Tech Stack",
                    "description": "Optional: what stack is in use? e.g. 'FastAPI + React + PostgreSQL'",
                    "maxLength": 300,
                },
                "audience": {
                    "type": "string",
                    "title": "Audience",
                    "description": "Who is the spec for?",
                    "default": "engineers",
                    "enum": ["engineers", "product", "both"],
                },
                "context": {
                    "type": "string",
                    "title": "System Context",
                    "description": "Optional: relevant context about the existing system.",
                    "maxLength": 600,
                },
            },
            required=["requirements"],
        ),
        "output_schema": _output_schema_object(
            {
                "title": {"type": "string"},
                "format": {"type": "string"},
                "sections": {"type": "array", "items": {"type": "object"}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
                "out_of_scope": {"type": "array", "items": {"type": "string"}},
                "estimated_complexity": {"type": "string"},
                "full_text": {"type": "string"},
            },
            required=["title", "format", "sections", "full_text"],
        ),
        "output_examples": [
            {
                "input": {"requirements": "Add two-factor authentication via TOTP to the login flow.", "format": "rfc"},
                "output": {
                    "title": "RFC: TOTP Two-Factor Authentication",
                    "format": "rfc",
                    "sections": [{"heading": "Motivation", "content": "Passwords alone are insufficient..."}],
                    "open_questions": ["Which TOTP libraries to use?", "How to handle backup codes?"],
                    "out_of_scope": ["SMS-based 2FA", "Hardware keys"],
                    "estimated_complexity": "M",
                    "full_text": "# RFC: TOTP Two-Factor Authentication\n\n## Motivation\n...",
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
