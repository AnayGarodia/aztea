"""Second chunk of built-in agent specs (argument to `specs.extend([...])`)."""
from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    ARXIV_RESEARCH_AGENT_ID as _ARXIV_RESEARCH_AGENT_ID,
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
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
        "description": "Generates production image artifacts with real model backends (OpenAI gpt-image-1 or configured Replicate model), with optional reference-image guidance.",
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
        "description": "Turns a creative brief into production-ready shot plans and generates an actual video artifact through a configured video model backend.",
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
    "description": "Search real academic papers on arXiv and get an expert synthesis: key themes, seminal works, open questions, and suggested follow-ups. Pulls live data from arXiv.org — not LLM hallucinations.",
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
        },
        required=["query", "papers", "synthesis"],
    ),
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
    "description": "Execute Python code in a sandboxed environment and get stdout, stderr, exit code, execution time, and an expert explanation. Supports math, data manipulation, algorithms, and any pure-Python computation.",
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
    "description": "Fetch any public URL and return a structured analysis: dense summary, key points, direct answers to your question, verbatim supporting quotes, and extracted links. Reads the actual page — not a cached version.",
    "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_WEB_RESEARCHER_AGENT_ID],
    "price_per_call_usd": 0.01,
    "tags": ["web", "research", "summarization", "content-extraction"],
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Public URL to fetch and analyze", "example": "https://en.wikipedia.org/wiki/Large_language_model"},
            "question": {"type": "string", "default": "", "description": "Specific question to answer from the page content"},
            "mode": {"type": "string", "enum": ["summary", "extract", "qa"], "default": "summary", "description": "Analysis mode"},
        },
        "required": ["url"],
    },
    "output_schema": _output_schema_object(
        {
            "url": {"type": "string"},
            "title": {"type": "string"},
            "word_count": {"type": "integer"},
            "fetched_at": {"type": "string"},
            "summary": {"type": "string"},
            "key_points": {"type": "array", "items": {"type": "string"}},
            "answer": {"type": "string"},
            "quotes": {"type": "array", "items": {"type": "string"}},
            "links": {"type": "array", "items": {"type": "object"}},
            "content_type": {"type": "string"},
        },
        required=["url", "summary"],
    ),
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
    ]
