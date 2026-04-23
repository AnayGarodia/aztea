# server.application shard 3 — protocol envelope + work-example helpers:
# artifact normalisation, typed job-message payload validation, SSE stream
# fan-out, job-event recording, idempotency-key bookkeeping, outbound URL
# validation, rate-limited hook URL checks. No routes here.


def _normalize_input_protocol_from_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    protocol = payload.get("protocol")
    if not isinstance(protocol, dict):
        private_task = _normalize_optional_bool(payload.get("private_task"), field_name="private_task")
        if private_task is None:
            return dict(payload), []
        normalized_payload = _merge_protocol_input_envelope(
            payload,
            private_task=private_task,
        )
        return normalized_payload, []
    input_artifacts = _normalize_protocol_artifact_list(
        protocol.get("input_artifacts"),
        field_name="protocol.input_artifacts",
    )
    preferred_input_formats = _normalize_format_preferences(
        protocol.get("preferred_input_formats"),
        field_name="protocol.preferred_input_formats",
    )
    preferred_output_formats = _normalize_format_preferences(
        protocol.get("preferred_output_formats"),
        field_name="protocol.preferred_output_formats",
    )
    communication_channel = _normalize_protocol_channel(
        protocol.get("communication_channel"),
        field_name="protocol.communication_channel",
    )
    private_task = _normalize_optional_bool(
        protocol.get("private_task", payload.get("private_task")),
        field_name="protocol.private_task",
    )
    metadata = _normalize_protocol_metadata(protocol.get("metadata"), field_name="protocol.metadata")
    normalized_payload = _merge_protocol_input_envelope(
        payload,
        input_artifacts=input_artifacts,
        preferred_input_formats=preferred_input_formats,
        preferred_output_formats=preferred_output_formats,
        communication_channel=communication_channel,
        protocol_metadata=metadata,
        private_task=private_task,
    )
    return normalized_payload, preferred_output_formats


def _normalize_output_protocol_for_response(
    response_payload: Any,
    *,
    requested_output_formats: list[str] | None = None,
) -> Any:
    if not isinstance(response_payload, dict):
        return response_payload
    normalized = dict(response_payload)
    protocol = normalized.get("protocol")
    protocol_dict = dict(protocol) if isinstance(protocol, dict) else {}
    artifact_candidates = protocol_dict.get("output_artifacts")
    if not artifact_candidates:
        artifact_candidates = normalized.get("artifacts")
    output_artifacts = _normalize_protocol_artifact_list(
        artifact_candidates,
        field_name="protocol.output_artifacts",
        strict=False,
    )
    output_format = str(protocol_dict.get("output_format") or "").strip().lower() or None
    if output_format is None and output_artifacts:
        output_format = str(output_artifacts[0].get("mime") or "").strip().lower() or None
    metadata = _normalize_protocol_metadata(protocol_dict.get("metadata"), field_name="protocol.metadata")
    if requested_output_formats:
        metadata.setdefault("requested_output_formats", list(requested_output_formats))
    return _merge_protocol_output_envelope(
        normalized,
        output_artifacts=output_artifacts,
        output_format=output_format,
        protocol_metadata=metadata,
    )


def _is_private_task_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    try:
        private_top_level = _normalize_optional_bool(payload.get("private_task"), field_name="private_task")
    except ValueError:
        private_top_level = None
    if private_top_level is True:
        return True
    protocol = payload.get("protocol")
    if not isinstance(protocol, dict):
        return False
    try:
        private_protocol = _normalize_optional_bool(
            protocol.get("private_task"),
            field_name="protocol.private_task",
        )
    except ValueError:
        private_protocol = None
    return bool(private_protocol)


def _truncate_example_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= _AGENT_WORK_EXAMPLE_MAX_STRING_LEN:
            return value
        return value[:_AGENT_WORK_EXAMPLE_MAX_STRING_LEN] + "...<truncated>"
    if isinstance(value, list):
        return [_truncate_example_value(item) for item in value[:20]]
    if isinstance(value, dict):
        truncated: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            key_text = str(key)
            if key_text == "url_or_base64" and isinstance(item, str) and item.startswith("data:"):
                truncated[key_text] = "<inline-data-uri-omitted>"
                continue
            truncated[key_text] = _truncate_example_value(item)
        return truncated
    return value


def _extract_protocol_output_artifacts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    protocol = payload.get("protocol")
    protocol_dict = dict(protocol) if isinstance(protocol, dict) else {}
    artifact_candidates = protocol_dict.get("output_artifacts")
    if artifact_candidates is None:
        artifact_candidates = payload.get("artifacts")
    return _normalize_protocol_artifact_list(
        artifact_candidates,
        field_name="output_artifacts",
        strict=False,
    )


def _record_public_work_example(
    agent: dict,
    input_payload: Any,
    output_payload: Any,
    *,
    job_id: str | None = None,
    latency_ms: float | None = None,
    quality_score: int | None = None,
    rating: int | None = None,
) -> None:
    if not isinstance(agent, dict):
        return
    if _is_private_task_payload(input_payload):
        return
    if not isinstance(input_payload, dict) or not isinstance(output_payload, dict):
        return
    agent_id = str(agent.get("agent_id") or "").strip()
    if not agent_id:
        return
    artifacts = _extract_protocol_output_artifacts(output_payload)
    example: dict[str, Any] = {
        "created_at": _utc_now_iso(),
        "input": _truncate_example_value(input_payload),
        "output": _truncate_example_value(output_payload),
        "model_provider": str(agent.get("model_provider") or "").strip().lower() or None,
        "model_id": str(agent.get("model_id") or "").strip() or None,
    }
    if job_id:
        example["job_id"] = str(job_id)
    if latency_ms is not None:
        example["latency_ms"] = round(float(latency_ms), 1)
    if quality_score is not None:
        example["quality_score"] = int(quality_score)
    if rating is not None:
        example["rating"] = int(rating)
    if artifacts:
        example["artifacts"] = _truncate_example_value(artifacts)
    try:
        registry.append_agent_output_example(
            agent_id,
            example,
            max_examples=_AGENT_WORK_EXAMPLES_MAX,
        )
    except Exception:
        _LOG.exception("Failed to append output example for agent %s.", agent_id)


def _normalize_job_message_protocol(
    raw_type: str,
    raw_payload: dict,
    correlation_id: str | None = None,
) -> dict:
    msg_type = str(raw_type or "").strip().lower()
    if not msg_type:
        raise ValueError("type must not be empty")
    if not isinstance(raw_payload, dict):
        raise ValueError("payload must be an object.")

    parsed = _parse_job_message_protocol_from_models(msg_type, raw_payload, correlation_id)
    if parsed is None:
        parsed = _parse_job_message_protocol_fallback(msg_type, raw_payload, correlation_id)

    normalized_type = str(parsed.get("type") or "").strip().lower()
    payload = parsed.get("payload", {})
    normalized_correlation = parsed.get("correlation_id")
    if not normalized_type:
        raise ValueError("type must not be empty")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object.")

    if normalized_type in _LEGACY_JOB_MESSAGE_TYPES:
        _LOG.warning(
            "Deprecated legacy job message contract used for type '%s'; prefer typed protocol.",
            normalized_type,
        )
        return {
            "type": normalized_type,
            "payload": payload,
            "correlation_id": normalized_correlation,
            "legacy_type": normalized_type,
        }

    if normalized_type in _TYPED_JOB_MESSAGE_TYPES:
        return {
            "type": normalized_type,
            "payload": payload,
            "correlation_id": normalized_correlation,
            "legacy_type": None,
        }

    raise ValueError(f"Unsupported job message type: {normalized_type}")


def _parse_job_message_protocol_from_models(
    msg_type: str,
    payload: dict,
    correlation_id: str | None,
) -> dict | None:
    normalize_helper = getattr(core_models, "normalize_job_message_body", None)
    if not callable(normalize_helper):
        return None

    try:
        normalized = normalize_helper(
            msg_type=msg_type,
            payload=payload,
            correlation_id=correlation_id,
            allow_legacy=True,
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(str(exc)) from exc

    normalized_type = str(normalized.get("type") or msg_type).strip().lower()
    canonical_type = str(normalized.get("canonical_type") or normalized_type).strip().lower()
    normalized_payload = normalized.get("payload", payload)
    normalized_correlation = normalized.get("correlation_id")
    if not isinstance(normalized_payload, dict):
        raise ValueError("payload must be an object.")

    if normalized_type in _LEGACY_JOB_MESSAGE_TYPES:
        return {
            "type": normalized_type,
            "payload": normalized_payload,
            "correlation_id": normalized_correlation,
        }
    return {
        "type": canonical_type,
        "payload": normalized_payload,
        "correlation_id": normalized_correlation,
    }


def _parse_job_message_protocol_fallback(
    msg_type: str,
    payload: dict,
    correlation_id: str | None,
) -> dict:
    normalized_correlation = None
    if correlation_id is not None:
        text = str(correlation_id).strip()
        normalized_correlation = text or None

    if msg_type in _TYPED_JOB_MESSAGE_TYPES:
        validated_payload = _validate_typed_job_message_payload(msg_type, payload)
        return {
            "type": msg_type,
            "payload": validated_payload,
            "correlation_id": normalized_correlation,
        }

    if msg_type in _LEGACY_JOB_MESSAGE_TYPES:
        return {"type": msg_type, "payload": dict(payload), "correlation_id": normalized_correlation}

    raise ValueError(f"Unsupported job message type: {msg_type}")


def _validate_typed_job_message_payload(msg_type: str, payload: dict) -> dict:
    normalized = dict(payload)

    def _required_text(field: str, label: str | None = None) -> str:
        key = label or field
        value = str(normalized.get(field, "")).strip()
        if not value:
            raise ValueError(f"{msg_type} payload.{key} is required.")
        return value

    if msg_type == "clarification_request":
        normalized["question"] = _required_text("question")
        return normalized

    if msg_type == "clarification_response":
        normalized["answer"] = _required_text("answer")
        return normalized

    if msg_type == "note":
        text = str(
            normalized.get("message")
            or normalized.get("note")
            or normalized.get("text")
            or ""
        ).strip()
        if not text:
            raise ValueError("note payload.text is required.")
        normalized["text"] = text
        return normalized

    if msg_type == "progress":
        percent_raw = normalized.get("percent")
        if percent_raw is None:
            raise ValueError("progress payload.percent is required.")
        if percent_raw is not None:
            try:
                percent = int(percent_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("progress payload.percent must be an integer between 0 and 100.") from exc
            if percent < 0 or percent > 100:
                raise ValueError("progress payload.percent must be an integer between 0 and 100.")
            normalized["percent"] = percent
        note = str(normalized.get("note") or "").strip()
        if note:
            normalized["note"] = note
        return normalized

    if msg_type == "agent_message":
        channel = str(normalized.get("channel") or "").strip()
        if not channel:
            raise ValueError("agent_message payload.channel is required.")
        normalized["channel"] = channel
        body = normalized.get("body")
        if isinstance(body, str):
            body_text = body.strip()
            if not body_text:
                raise ValueError("agent_message payload.body must not be empty.")
            normalized["body"] = body_text
        elif isinstance(body, dict):
            normalized["body"] = dict(body)
        else:
            raise ValueError("agent_message payload.body must be an object or non-empty string.")
        to_id = str(normalized.get("to_id") or "").strip()
        if to_id:
            normalized["to_id"] = to_id
        else:
            normalized.pop("to_id", None)
        return normalized

    if msg_type == "tool_call":
        tool_name = str(normalized.get("tool_name") or normalized.get("name") or "").strip()
        if not tool_name:
            raise ValueError("tool_call payload.tool_name is required.")
        normalized["tool_name"] = tool_name
        args = normalized.get("args")
        if args is None:
            normalized["args"] = {}
        elif not isinstance(args, dict):
            raise ValueError("tool_call payload.args must be an object.")
        correlation_id = str(normalized.get("correlation_id") or "").strip()
        if correlation_id:
            normalized["correlation_id"] = correlation_id
        else:
            normalized.pop("correlation_id", None)
        return normalized

    if msg_type == "tool_result":
        correlation_id = str(normalized.get("correlation_id") or "").strip()
        if not correlation_id:
            raise ValueError("tool_result payload.correlation_id is required.")
        normalized["correlation_id"] = correlation_id
        result_payload = normalized.get("payload")
        if result_payload is None:
            normalized["payload"] = {}
        elif not isinstance(result_payload, dict):
            raise ValueError("tool_result payload.payload must be an object.")
        return normalized

    raise ValueError(f"Unsupported typed message type: {msg_type}")


def _job_has_tool_call_correlation(job_id: str, correlation_id: str) -> bool:
    helper = getattr(jobs, "tool_call_correlation_exists", None)
    if callable(helper):
        try:
            return bool(helper(job_id, correlation_id))
        except Exception as exc:
            _LOG.warning(
                "Failed to query tool-call correlation index for job %s correlation %s: %s",
                job_id,
                correlation_id,
                exc,
            )

    since_id: int | None = None
    while True:
        batch = jobs.get_messages(job_id, since_id=since_id, limit=200)
        if not batch:
            return False
        for item in batch:
            if item.get("type") != "tool_call":
                continue
            payload = item.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if str(payload.get("correlation_id") or "").strip() == correlation_id:
                return True
        if len(batch) < 200:
            return False
        since_id = int(batch[-1]["message_id"])


def _subscribe_job_stream(job_id: str) -> Queue:
    return jobs.subscribe_job_messages(job_id)


def _unsubscribe_job_stream(job_id: str, subscriber: Queue) -> None:
    jobs.unsubscribe_job_messages(job_id, subscriber)


def _job_message_to_sse(message: dict) -> str:
    event_id = message.get("message_id")
    payload = json.dumps(message, separators=(",", ":"), default=str)
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append("event: message")
    for line in payload.splitlines():
        lines.append(f"data: {line}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _event_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        payload = json.loads(d.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    d["payload"] = payload if isinstance(payload, dict) else {}
    return d


def _record_job_event(
    job: dict | None,
    event_type: str,
    actor_owner_id: str | None = None,
    payload: dict | None = None,
) -> dict | None:
    if job is None:
        return None

    try:
        payload_json = json.dumps(payload or {})
    except TypeError:
        payload_json = json.dumps({"value": str(payload)})

    created_at = _utc_now_iso()
    with jobs._conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO job_events
                (job_id, agent_id, agent_owner_id, caller_owner_id,
                 event_type, actor_owner_id, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["job_id"],
                job["agent_id"],
                job["agent_owner_id"],
                job["caller_owner_id"],
                event_type,
                actor_owner_id,
                payload_json,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM job_events WHERE event_id = ?",
            (cur.lastrowid,),
        ).fetchone()

    event = _event_row_to_dict(row)
    logging_utils.log_event(
        _LOG,
        logging.INFO,
        "job.state_transition",
        {
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "job_id": event.get("job_id"),
            "agent_id": event.get("agent_id"),
            "actor_owner_id": event.get("actor_owner_id"),
            "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
        },
    )
    _deliver_job_event_hooks(event)
    if event.get("event_type") in {"job.completed", "job.failed", "job.failed_quality"} and (job or {}).get("callback_url"):
        _enqueue_job_callback(job, event["event_id"])
    return event


def _stable_json_text(payload: Any) -> str:
    try:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    except TypeError:
        return json.dumps({"value": str(payload)}, separators=(",", ":"), sort_keys=True)


def _idempotency_begin(
    request: Request,
    caller: core_models.CallerContext,
    scope: str,
    payload: Any,
) -> dict | None:
    idempotency_key = (request.headers.get(_IDEMPOTENCY_KEY_HEADER, "") or "").strip()
    if not idempotency_key:
        return None
    if len(idempotency_key) > 128:
        raise HTTPException(status_code=422, detail=f"{_IDEMPOTENCY_KEY_HEADER} is too long.")

    owner_id = caller["owner_id"]
    request_hash = hashlib.sha256(_stable_json_text(payload).encode("utf-8")).hexdigest()
    now = _utc_now_iso()

    with jobs._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT request_hash, status, response_status, response_body
            FROM idempotency_requests
            WHERE owner_id = ? AND scope = ? AND idempotency_key = ?
            """,
            (owner_id, scope, idempotency_key),
        ).fetchone()
        if row is not None:
            if row["request_hash"] != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{_IDEMPOTENCY_KEY_HEADER} was already used for a different request payload."
                    ),
                )
            if row["status"] == "completed":
                try:
                    replay_body = json.loads(row["response_body"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    replay_body = error_codes.make_error(
                        error_codes.INVALID_INPUT,
                        "Stored idempotent response is invalid.",
                    )
                replay_status = int(row["response_status"] or 200)
                return {
                    "replay": True,
                    "status_code": replay_status,
                    "body": replay_body,
                }
            raise HTTPException(
                status_code=409,
                detail=f"A request with this {_IDEMPOTENCY_KEY_HEADER} is still in progress.",
            )

        conn.execute(
            """
            INSERT INTO idempotency_requests
                (owner_id, scope, idempotency_key, request_hash, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'in_progress', ?, ?)
            """,
            (owner_id, scope, idempotency_key, request_hash, now, now),
        )

    return {
        "replay": False,
        "owner_id": owner_id,
        "scope": scope,
        "idempotency_key": idempotency_key,
    }


def _idempotency_complete(idempotency_state: dict | None, body: Any, status_code: int) -> None:
    if not idempotency_state or idempotency_state.get("replay"):
        return
    now = _utc_now_iso()
    with jobs._conn() as conn:
        conn.execute(
            """
            UPDATE idempotency_requests
            SET status = 'completed',
                response_status = ?,
                response_body = ?,
                updated_at = ?
            WHERE owner_id = ? AND scope = ? AND idempotency_key = ? AND status = 'in_progress'
            """,
            (
                int(status_code),
                _stable_json_text(body),
                now,
                idempotency_state["owner_id"],
                idempotency_state["scope"],
                idempotency_state["idempotency_key"],
            ),
        )


def _idempotency_abort(idempotency_state: dict | None) -> None:
    if not idempotency_state or idempotency_state.get("replay"):
        return
    with jobs._conn() as conn:
        conn.execute(
            """
            DELETE FROM idempotency_requests
            WHERE owner_id = ? AND scope = ? AND idempotency_key = ? AND status = 'in_progress'
            """,
            (
                idempotency_state["owner_id"],
                idempotency_state["scope"],
                idempotency_state["idempotency_key"],
            ),
        )


def _run_idempotent_json_response(
    request: Request,
    caller: core_models.CallerContext,
    scope: str,
    payload: Any,
    operation: Callable[[], tuple[Any, int]],
) -> JSONResponse:
    idempotency_state = _idempotency_begin(request, caller, scope, payload)
    if idempotency_state and idempotency_state.get("replay"):
        return JSONResponse(
            content=idempotency_state["body"],
            status_code=int(idempotency_state["status_code"]),
        )

    try:
        body, status_code = operation()
    except Exception:
        _idempotency_abort(idempotency_state)
        raise

    _idempotency_complete(idempotency_state, body=body, status_code=status_code)
    return JSONResponse(content=body, status_code=status_code)


def _hook_row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _validate_outbound_url(target_url: str, field_name: str) -> str:
    return _url_security.validate_outbound_url(
        target_url,
        field_name,
        allow_private=_ALLOW_PRIVATE_OUTBOUND_URLS,
    )


def _validate_hook_url(target_url: str) -> str:
    return _validate_outbound_url(target_url, "target_url")


def _effective_port(scheme: str, port: int | None) -> int:
    if port is not None:
        return port
    return 443 if scheme == "https" else 80


def _allow_loopback_same_origin(request: Request, target_url: str) -> bool:
    parsed = urlparse(target_url.strip())
    target_host = (parsed.hostname or "").strip().lower()
    if target_host not in {"localhost", "127.0.0.1", "::1"}:
        return False

    request_host = (request.url.hostname or "").strip().lower()
    if request_host not in {"localhost", "127.0.0.1", "::1"}:
        return False

    target_scheme = (parsed.scheme or "").strip().lower()
    request_scheme = (request.url.scheme or "").strip().lower()
    if target_scheme != request_scheme:
        return False

    target_port = _effective_port(target_scheme, parsed.port)
    request_port = _effective_port(request_scheme, request.url.port)
    return target_port == request_port


def _validate_agent_endpoint_url(request: Request, endpoint_url: str) -> str:
    normalized = endpoint_url.strip()
    if _allow_loopback_same_origin(request, normalized):
        parsed = urlparse(normalized)
        if parsed.username or parsed.password:
            raise ValueError("endpoint_url must not include username or password.")
        if parsed.fragment:
            raise ValueError("endpoint_url must not include URL fragments.")
        return normalized
    return _validate_outbound_url(normalized, "endpoint_url")


def _probe_register_endpoint_or_400(url: str) -> None:
    """
    Liveness + agent-shape probe for an agent endpoint_url at registration time.

    Rejects if:
    - Connection fails or times out
    - Endpoint returns 5xx
    - POST probe returns an HTML page (indicates a website, not an agent API)
    """
    try:
        resp = http.head(url, timeout=5.0, allow_redirects=False)
        status = resp.status_code
        if status in (405, 501) or status >= 500:
            resp = http.get(url, timeout=5.0, allow_redirects=False)
            status = resp.status_code
        if status >= 500:
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.REGISTRY_ENDPOINT_UNREACHABLE,
                    f"Endpoint responded with status {status}. Provide a reachable URL.",
                ),
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.REGISTRY_ENDPOINT_UNREACHABLE,
                f"Could not reach endpoint: {type(exc).__name__}. Check the URL and make sure the server is running.",
            ),
        )

    # POST probe: check that the endpoint responds like an API, not a website.
    try:
        post_resp = http.post(
            url,
            json={},
            timeout=5.0,
            allow_redirects=False,
            headers={"Content-Type": "application/json"},
        )
        content_type = post_resp.headers.get("content-type", "").lower()
        if "text/html" in content_type:
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.REGISTRY_ENDPOINT_UNREACHABLE,
                    "Your endpoint returned an HTML page instead of JSON — it doesn't look like an agent API. "
                    "Make sure POST requests to this URL accept a JSON body and return a JSON response.",
                ),
            )
    except HTTPException:
        raise
    except Exception:
        pass  # POST probe failure is non-fatal; liveness check above already passed


def _create_job_event_hook(owner_id: str, target_url: str, secret: str | None = None) -> dict:
    hook_id = str(uuid.uuid4())
    now = _utc_now_iso()
    normalized_secret = secret.strip() if secret else None
    with jobs._conn() as conn:
        conn.execute(
            """
            INSERT INTO job_event_hooks
                (hook_id, owner_id, target_url, secret, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (hook_id, owner_id, _validate_hook_url(target_url), normalized_secret, now),
        )
        row = conn.execute(
            "SELECT * FROM job_event_hooks WHERE hook_id = ?",
            (hook_id,),
        ).fetchone()
    return _hook_row_to_dict(row)


def _list_job_event_hooks(owner_id: str | None = None, include_inactive: bool = False) -> list[dict]:
    clauses = []
    params: list[Any] = []
    if owner_id is not None:
        clauses.append("owner_id = ?")
        params.append(owner_id)
    if not include_inactive:
        clauses.append("is_active = 1")
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with jobs._conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM job_event_hooks
            {where_sql}
            ORDER BY created_at DESC
            """,
            tuple(params),
        ).fetchall()
    return [_hook_row_to_dict(r) for r in rows]


def _deactivate_job_event_hook(hook_id: str, owner_id: str | None = None) -> bool:
    now = _utc_now_iso()
    with jobs._conn() as conn:
        if owner_id is None:
            result = conn.execute(
                "UPDATE job_event_hooks SET is_active = 0 WHERE hook_id = ?",
                (hook_id,),
            )
        else:
            result = conn.execute(
                "UPDATE job_event_hooks SET is_active = 0 WHERE hook_id = ? AND owner_id = ?",
                (hook_id, owner_id),
            )
        if result.rowcount <= 0:
            return False
        conn.execute(
            """
            UPDATE job_event_deliveries
            SET status = 'cancelled',
                next_attempt_at = ?,
                updated_at = ?,
                last_error = COALESCE(last_error, 'hook deactivated')
            WHERE hook_id = ?
              AND status = 'pending'
            """,
            (now, now, hook_id),
        )
    return True


def _deliver_job_event_hooks(event: dict) -> None:
    _enqueue_job_event_hook_deliveries(event)


def _set_hook_worker_state(**updates: Any) -> None:
    with _HOOK_WORKER_STATE_LOCK:
        _HOOK_WORKER_STATE.update(updates)


def _set_builtin_worker_state(**updates: Any) -> None:
    with _BUILTIN_WORKER_STATE_LOCK:
        _BUILTIN_WORKER_STATE.update(updates)


def _set_dispute_judge_state(**updates: Any) -> None:
    with _DISPUTE_JUDGE_STATE_LOCK:
        _DISPUTE_JUDGE_STATE.update(updates)


def _set_payments_reconciliation_state(**updates: Any) -> None:
    with _PAYMENTS_RECONCILIATION_STATE_LOCK:
        _PAYMENTS_RECONCILIATION_STATE.update(updates)


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _resolve_builtin_agent_id(agent: dict[str, Any]) -> str | None:
    endpoint = _normalize_endpoint_ref(str(agent.get("endpoint_url") or ""))
    matched = _BUILTIN_ENDPOINT_TO_AGENT_ID.get(endpoint)
    if matched:
        return matched
    agent_id = str(agent.get("agent_id") or "").strip()
    if agent_id in _BUILTIN_AGENT_IDS and endpoint.startswith("internal://"):
        return agent_id
    return None


