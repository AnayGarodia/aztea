"""Second chunk of built-in agent specs (argument to `specs.extend([...])`)."""
from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    ARXIV_RESEARCH_AGENT_ID as _ARXIV_RESEARCH_AGENT_ID,
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
    HEALTHCARE_EXPERT_AGENT_ID as _HEALTHCARE_EXPERT_AGENT_ID,
    IMAGE_GENERATOR_AGENT_ID as _IMAGE_GENERATOR_AGENT_ID,
    INCIDENT_RESPONSE_AGENT_ID as _INCIDENT_RESPONSE_AGENT_ID,
    PYTHON_EXECUTOR_AGENT_ID as _PYTHON_EXECUTOR_AGENT_ID,
    SYSTEM_DESIGN_AGENT_ID as _SYSTEM_DESIGN_AGENT_ID,
    VIDEO_STORYBOARD_AGENT_ID as _VIDEO_STORYBOARD_AGENT_ID,
    WEB_RESEARCHER_AGENT_ID as _WEB_RESEARCHER_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object


def load_builtin_specs_part2() -> list[dict[str, Any]]:
    return [
    {
        "agent_id": _SYSTEM_DESIGN_AGENT_ID,
        "name": "System Design Reviewer Agent",
        "description": "Principal-level architecture planning with request flows, data models, tradeoff matrices, phased rollout plans, and explicit risk ownership.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SYSTEM_DESIGN_AGENT_ID],
        "price_per_call_usd": 0.08,
        "tags": ["architecture", "system-design", "scalability", "reliability"],
        "input_schema": {
            "type": "object",
            "properties": {
                "context": {"type": "string", "description": "Product/service context and objective."},
                "requirements": {"type": "array", "items": {"type": "string"}},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "scale_assumptions": {"type": "array", "items": {"type": "string"}},
                "stack": {"type": "array", "items": {"type": "string"}},
                "non_functional_requirements": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["context", "requirements"],
        },
        "output_schema": _output_schema_object(
            {
                "architecture_summary": {"type": "string"},
                "components": {"type": "array", "items": {"type": "object"}},
                "request_flow": {"type": "array", "items": {"type": "string"}},
                "tradeoff_matrix": {"type": "array", "items": {"type": "object"}},
                "scaling_plan": {"type": "object"},
                "phase_plan": {"type": "array", "items": {"type": "object"}},
                "top_risks": {"type": "array", "items": {"type": "object"}},
            },
            required=["architecture_summary", "components", "phase_plan"],
        ),
        "output_examples": [
            {
                "input": {
                    "context": "Realtime fraud scoring API for card transactions.",
                    "requirements": ["p95 under 120ms", "99.95% availability", "full auditability"],
                    "constraints": ["single region initially", "lean infra team"],
                },
                "output": {
                    "architecture_summary": "Event-driven scoring service with low-latency cache and async model updates.",
                    "components": [
                        {"name": "gateway", "responsibility": "auth + rate limit"},
                        {"name": "scoring-service", "responsibility": "rules + model inference"},
                    ],
                    "request_flow": ["gateway validates request", "scoring-service reads feature cache", "decision emitted to ledger"],
                    "tradeoff_matrix": [
                        {
                            "decision": "cache strategy",
                            "option_a": "global cache",
                            "option_b": "service-local cache",
                            "chosen": "service-local cache",
                            "rationale": "lower latency and reduced blast radius",
                        }
                    ],
                    "scaling_plan": {"hotspots": ["feature lookup"], "mitigations": ["read-through cache"]},
                    "phase_plan": [{"phase": "phase-1", "goal": "stabilize core path"}],
                    "top_risks": [{"risk": "feature staleness", "impact": "medium", "owner": "platform"}],
                },
            }
        ],
    },
    {
        "agent_id": _INCIDENT_RESPONSE_AGENT_ID,
        "name": "Incident Response Commander Agent",
        "description": "SRE-grade incident command: likely root causes with confidence, first-15-minute actions, stabilization plan, communication templates, and 30/60/90-minute timeline.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_INCIDENT_RESPONSE_AGENT_ID],
        "price_per_call_usd": 0.08,
        "tags": ["incident-response", "sre", "operations", "reliability"],
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_title": {"type": "string"},
                "severity": {"type": "string", "default": "unknown"},
                "symptoms": {"type": "array", "items": {"type": "string"}},
                "service_map": {"type": "array", "items": {"type": "string"}},
                "recent_changes": {"type": "array", "items": {"type": "string"}},
                "telemetry": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["incident_title", "symptoms"],
        },
        "output_schema": _output_schema_object(
            {
                "severity_assessment": {"type": "object"},
                "probable_root_causes": {"type": "array", "items": {"type": "object"}},
                "first_15_min_actions": {"type": "array", "items": {"type": "string"}},
                "stabilization_plan": {"type": "array", "items": {"type": "string"}},
                "communications": {"type": "object"},
                "timeline_30_60_90": {"type": "object"},
                "postmortem_followups": {"type": "array", "items": {"type": "string"}},
            },
            required=["severity_assessment", "first_15_min_actions", "communications"],
        ),
        "output_examples": [
            {
                "input": {
                    "incident_title": "Checkout latency spike",
                    "symptoms": ["p95 jumped from 180ms to 1.9s", "timeouts from payment provider"],
                    "recent_changes": ["new retry policy deployed"],
                },
                "output": {
                    "severity_assessment": {"level": "sev-1", "justification": "Revenue path degraded globally"},
                    "probable_root_causes": [
                        {
                            "cause": "retry amplification to payment upstream",
                            "confidence": "high",
                            "evidence": ["timeout volume aligns with deploy"],
                        }
                    ],
                    "first_15_min_actions": ["freeze deploys", "disable aggressive retries", "enable load shedding"],
                    "stabilization_plan": ["rollback retry policy", "drain failing worker pool"],
                    "communications": {
                        "internal_update": "Mitigating checkout latency via retry rollback.",
                        "status_page_update": "Investigating elevated checkout errors.",
                        "next_update_eta": "15 minutes",
                    },
                    "timeline_30_60_90": {"30": "service stabilized", "60": "error budget impact estimated", "90": "postmortem owner assigned"},
                    "postmortem_followups": ["retry policy guardrail tests", "per-provider circuit breaker thresholds"],
                },
            }
        ],
    },
    {
        "agent_id": _HEALTHCARE_EXPERT_AGENT_ID,
        "name": "Healthcare Expert Agent",
        "description": "Clinical triage copilot for symptom assessment, urgency flags, and clinician-ready visit prep. Educational-only guidance with explicit emergency escalation.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_HEALTHCARE_EXPERT_AGENT_ID],
        "price_per_call_usd": 0.03,
        "tags": ["healthcare", "triage", "clinical-support", "patient-education"],
        "input_schema": _output_schema_object(
            {
                "symptoms": {"type": "array", "items": {"type": "string"}},
                "age_years": {"type": "integer", "minimum": 0},
                "sex": {"type": "string"},
                "medical_history": {"type": "array", "items": {"type": "string"}},
                "medications": {"type": "array", "items": {"type": "string"}},
                "duration": {"type": "string"},
                "urgency_context": {"type": "string"},
                "goal": {"type": "string"},
            },
            required=["symptoms"],
        ),
        "output_schema": _output_schema_object(
            {
                "triage_level": {"type": "string"},
                "possible_considerations": {"type": "array", "items": {"type": "object"}},
                "red_flags": {"type": "array", "items": {"type": "object"}},
                "next_steps": {"type": "array", "items": {"type": "string"}},
                "questions_for_clinician": {"type": "array", "items": {"type": "string"}},
                "disclaimer": {"type": "string"},
            },
            required=["triage_level", "next_steps", "disclaimer"],
        ),
        "output_examples": [
            {
                "input": {
                    "symptoms": ["fever", "sore throat", "fatigue"],
                    "age_years": 29,
                    "duration": "2 days",
                    "medical_history": ["asthma"],
                },
                "output": {
                    "triage_level": "primary_care_24h",
                    "possible_considerations": [
                        {"condition": "viral upper respiratory infection", "confidence": "medium"}
                    ],
                    "red_flags": [
                        {"flag": "trouble breathing", "why_urgent": "possible respiratory compromise"}
                    ],
                    "next_steps": ["Hydrate and rest", "Schedule clinician visit within 24 hours if symptoms persist"],
                    "questions_for_clinician": ["Should I adjust asthma meds while sick?"],
                    "disclaimer": "Educational guidance only, not a diagnosis or treatment plan.",
                },
            }
        ],
    },
    {
        "agent_id": _IMAGE_GENERATOR_AGENT_ID,
        "name": "Image Generator Agent",
        "description": "Generates production image artifacts with real model backends (OpenAI gpt-image-1 or configured Replicate model), with optional reference-image guidance.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_IMAGE_GENERATOR_AGENT_ID],
        "price_per_call_usd": 0.02,
        "tags": ["image-generation", "creative", "multimodal", "design"],
        "input_schema": _output_schema_object(
            {
                "prompt": {"type": "string"},
                "style": {"type": "string"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "output_format": {"type": "string"},
                "input_images": {"type": "array", "items": {"type": "object"}},
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
                "brief": {"type": "string"},
                "duration_seconds": {"type": "integer"},
                "aspect_ratio": {"type": "string"},
                "style": {"type": "string"},
                "reference_images": {"type": "array", "items": {"type": "object"}},
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
