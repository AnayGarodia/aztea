"""
agent_datainsights.py — Structured data analyst

Input:  {
  "data": "...",       # JSON array, CSV text, or key:value pairs (up to ~8KB)
  "question": "...",   # specific question or "general" for open-ended analysis
  "format": "json|csv|text"
}
Output: {
  "row_count": int,
  "column_count": int,
  "columns_detected": [{"name": str, "type": str, "null_pct": float}],
  "summary_stats": {column: {min, max, mean, median, p25, p75, unique_count}},
  "key_findings": [str],
  "anomalies": [{"description": str, "severity": "low|medium|high", "affected_rows": str}],
  "trends": [str],
  "answer_to_question": str,
  "recommendations": [str],
  "suggested_visualizations": [{"chart_type": str, "x": str, "y": str, "rationale": str}]
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a senior data analyst and statistician. Given raw data (JSON, CSV, or text),
you produce rigorous analysis: descriptive statistics, anomaly detection, trend identification,
and actionable recommendations.

You are careful about:
- Distinguishing correlation from causation
- Flagging when sample sizes are too small for conclusions
- Noting data quality issues (nulls, inconsistent formatting, outliers)
- Giving specific, evidence-backed findings not vague observations

Return only valid JSON — no markdown fences, no prose outside the JSON object."""

_USER = """\
Analyze this data and answer the question.

Question: {question}
Data format hint: {format}

DATA:
{data}

Return a JSON object with EXACTLY these fields:
{{
  "row_count": integer (number of records/rows detected),
  "column_count": integer (number of fields/columns detected),
  "columns_detected": list of objects with "name", "type" (numeric/categorical/datetime/text), "null_pct" (0.0-1.0),
  "summary_stats": object keyed by column name, each with "min", "max", "mean", "median", "unique_count" where applicable (use null for non-numeric fields),
  "key_findings": list of 3-6 specific, evidence-backed findings with numbers,
  "anomalies": list of objects with "description", "severity" (low/medium/high), "affected_rows" (e.g. "row 14" or "3 of 50 rows"),
  "trends": list of 2-4 trend observations (directional changes, patterns, seasonality),
  "answer_to_question": direct answer to the question asked, with supporting data points,
  "recommendations": list of 2-4 actionable next steps based on the data,
  "suggested_visualizations": list of 1-3 objects with "chart_type", "x", "y", "rationale"
}}"""


def run(payload: dict) -> dict:
    data = str(payload.get("data", "")).strip()
    question = str(payload.get("question", "general")).strip() or "general"
    fmt = str(payload.get("format", "json")).lower()

    if not data:
        return {"error": "data is required"}

    if fmt not in ("json", "csv", "text"):
        fmt = "json"

    if question == "general":
        question = "Provide a comprehensive analysis of this dataset — key patterns, anomalies, and insights."

    req = CompletionRequest(
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(
                role="user",
                content=_USER.format(
                    question=question,
                    format=fmt,
                    data=data[:7000],
                ),
            ),
        ],
        temperature=0.15,
        max_tokens=2000,
    )

    raw = run_with_fallback(req)
    text = raw.text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        return {"error": "parse_error", "raw": text[:500]}
