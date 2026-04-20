"""
agent_sqlbuilder.py — Natural language to production-quality SQL

Input:  {
  "question": "What were the top 5 products by revenue last quarter?",
  "schema": "CREATE TABLE orders (...);",   # optional DDL or table descriptions
  "dialect": "postgresql|mysql|sqlite|bigquery|snowflake",
  "context": ""    # optional: data volume hints, performance requirements
}
Output: {
  "sql": str,
  "explanation": str,
  "assumptions": [str],
  "edge_cases": [str],
  "performance_notes": [str],
  "alternative_approaches": [{"description": str, "sql": str}],
  "estimated_complexity": "simple|moderate|complex",
  "dialect_specific_notes": str
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a principal database engineer with 15+ years building data platforms at companies
processing billions of rows. You write SQL that is correct on the first run, performant at
scale, and readable by humans.

You know:
- Window functions, CTEs, lateral joins, materialized CTEs, recursive CTEs
- Index usage and query plan pitfalls (index scans vs seeks, implicit type casts, function on indexed columns)
- Dialect differences: QUALIFY in BigQuery/Snowflake, LIMIT vs TOP vs ROWNUM, date function variants
- Null handling edge cases that silently wrong-answer most junior queries
- When to use subqueries vs CTEs vs temp tables vs window functions

Return only valid JSON — no markdown fences, no prose outside the JSON object."""

_USER = """\
Convert this natural language question into SQL.

Question: {question}
SQL dialect: {dialect}
{schema_section}
{context_section}

Return a JSON object with EXACTLY these fields:
{{
  "sql": "the complete, runnable SQL query — use CTEs for readability when helpful",
  "explanation": "plain English walkthrough of what the query does and why it's structured this way",
  "assumptions": list of assumptions made about schema, data, or business logic,
  "edge_cases": list of data edge cases this query handles (nulls, empty sets, ties, etc.),
  "performance_notes": list of performance considerations and index recommendations,
  "alternative_approaches": list of 0-2 objects each with:
    "description": why you might choose this approach instead,
    "sql": the alternative query,
  "estimated_complexity": one of "simple" | "moderate" | "complex",
  "dialect_specific_notes": any syntax that is dialect-specific and what to change for other dialects
}}"""


def run(payload: dict) -> dict:
    question = str(payload.get("question", "")).strip()
    schema = str(payload.get("schema", "")).strip()
    dialect = str(payload.get("dialect", "postgresql")).lower()
    context = str(payload.get("context", "")).strip()

    if not question:
        return {"error": "question is required"}

    valid_dialects = ("postgresql", "mysql", "sqlite", "bigquery", "snowflake")
    if dialect not in valid_dialects:
        dialect = "postgresql"

    schema_section = (
        f"\nDatabase schema:\n{schema[:3000]}\n"
        if schema
        else "\n(No schema provided — infer reasonable table/column names from the question.)\n"
    )
    context_section = f"\nAdditional context: {context[:500]}\n" if context else ""

    req = CompletionRequest(
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(
                role="user",
                content=_USER.format(
                    question=question,
                    dialect=dialect,
                    schema_section=schema_section,
                    context_section=context_section,
                ),
            ),
        ],
        temperature=0.1,
        max_tokens=1600,
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
