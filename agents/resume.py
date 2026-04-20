"""
agent_resume.py — Resume & career document analyzer

Input:  {
  "resume_text": "...",
  "job_description": "",    # optional: JD to match against
  "role_level": "junior|mid|senior|executive"
}
Output: {
  "overall_score": int (0-100),
  "ats_score": int (0-100),
  "keyword_match_score": int (0-100),
  "sections_found": [str],
  "sections_missing": [str],
  "strengths": [str],
  "critical_gaps": [str],
  "line_edits": [{"original": str, "improved": str, "reason": str}],
  "skills_present": [str],
  "skills_missing": [str],
  "formatting_notes": [str],
  "verdict": str,
  "one_line_summary": str
}
"""

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a senior technical recruiter and career coach who has reviewed 10,000+ resumes at
FAANG companies, top startups, and Fortune 500 firms. You know exactly what ATS systems flag,
what hiring managers skip, and which bullet points get callbacks.

Your reviews are honest and specific — no vague advice like "quantify your achievements".
You give rewritten bullet point examples, exact keyword gaps, and a concrete verdict.

Return only valid JSON — no markdown fences, no prose outside the JSON object."""

_USER = """\
Analyze this resume. Role level: {role_level}.
{jd_section}

RESUME:
{resume_text}

Return a JSON object with EXACTLY these fields:
{{
  "overall_score": integer 0-100 (holistic quality: impact, clarity, relevance, format),
  "ats_score": integer 0-100 (how well it passes ATS parsing: standard sections, no tables/columns, keywords),
  "keyword_match_score": integer 0-100 (0 if no JD provided),
  "sections_found": list of section names present (e.g. ["Summary","Experience","Education","Skills"]),
  "sections_missing": list of sections that should be there but are absent,
  "strengths": list of 3-5 specific things done well,
  "critical_gaps": list of 3-5 specific high-priority problems that reduce callbacks,
  "line_edits": list of up to 5 objects, each with:
    "original": exact weak phrase from the resume,
    "improved": rewritten version,
    "reason": why this change matters (1 sentence),
  "skills_present": list of technical/hard skills explicitly mentioned,
  "skills_missing": list of skills commonly required for this role level that are absent,
  "formatting_notes": list of formatting issues (too long, bad fonts, tables, etc),
  "verdict": one of "strong" | "passable" | "needs_work" | "major_revision",
  "one_line_summary": single sentence a recruiter would say to a hiring manager about this candidate
}}"""


def run(payload: dict) -> dict:
    resume_text = str(payload.get("resume_text", "")).strip()
    job_description = str(payload.get("job_description", "")).strip()
    role_level = str(payload.get("role_level", "mid")).lower()

    if not resume_text:
        return {"error": "resume_text is required"}

    if role_level not in ("junior", "mid", "senior", "executive"):
        role_level = "mid"

    jd_section = (
        f"\nJOB DESCRIPTION TO MATCH AGAINST:\n{job_description[:2000]}\n"
        if job_description
        else "\n(No job description provided — general analysis only.)\n"
    )

    req = CompletionRequest(
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(
                role="user",
                content=_USER.format(
                    role_level=role_level,
                    jd_section=jd_section,
                    resume_text=resume_text[:6000],
                ),
            ),
        ],
        temperature=0.2,
        max_tokens=1800,
    )

    raw = run_with_fallback(req)
    text = raw.text.strip()

    # Strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        return {"error": "parse_error", "raw": text[:500]}
