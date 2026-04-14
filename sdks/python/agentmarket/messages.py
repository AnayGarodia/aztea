from __future__ import annotations

from typing import TYPE_CHECKING

from .types import JSONObject

if TYPE_CHECKING:
    from .jobs import JobsNamespace


def ask_clarification(
    jobs: "JobsNamespace",
    job_id: str,
    question: str,
    schema: JSONObject | None = None,
) -> JSONObject:
    payload: JSONObject = {"question": question}
    if schema is not None:
        payload["schema"] = schema
    return jobs.post_message(job_id, "clarification_request", payload)


def answer_clarification(
    jobs: "JobsNamespace",
    job_id: str,
    answer: JSONObject | str,
    request_message_id: int,
) -> JSONObject:
    return jobs.post_message(
        job_id,
        "clarification_response",
        {"answer": answer, "request_message_id": request_message_id},
    )


def send_progress(jobs: "JobsNamespace", job_id: str, percent: int, note: str | None = None) -> JSONObject:
    payload: JSONObject = {"percent": percent}
    if note:
        payload["note"] = note
    return jobs.post_message(job_id, "progress", payload)


def send_partial_result(jobs: "JobsNamespace", job_id: str, payload: JSONObject) -> JSONObject:
    return jobs.post_message(job_id, "partial_result", {"payload": payload, "is_final": False})


def send_note(jobs: "JobsNamespace", job_id: str, text: str) -> JSONObject:
    return jobs.post_message(job_id, "note", {"text": text})
