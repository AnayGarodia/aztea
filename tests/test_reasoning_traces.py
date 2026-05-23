"""Tests for core/reasoning_traces.py."""

from __future__ import annotations

import pytest

from core.reasoning_traces import TraceRecorder


def test_records_ordered_steps():
    trace = TraceRecorder()
    with trace.step("retrieve", inputs_summary={"query": "abc"}):
        trace.record_outputs({"hits": 5})
        trace.record_llm_call()
    with trace.step("synthesise", inputs_summary={"hit_count": 5}):
        trace.record_llm_call()
        trace.record_outputs({"answer_len": 200})

    out = trace.to_dict()
    assert out["version"] == 1
    assert out["step_count"] == 2
    assert out["total_llm_calls"] == 2
    assert [s["name"] for s in out["steps"]] == ["retrieve", "synthesise"]
    assert out["steps"][0]["status"] == "ok"
    assert out["steps"][0]["llm_calls"] == 1
    assert out["steps"][0]["inputs_summary"] == {"query": "abc"}
    assert out["steps"][0]["outputs_summary"] == {"hits": 5}


def test_failed_step_records_error_and_propagates():
    trace = TraceRecorder()
    with pytest.raises(ValueError, match="boom"):
        with trace.step("crash", inputs_summary={"phase": "init"}):
            raise ValueError("boom")
    out = trace.to_dict()
    assert out["step_count"] == 1
    step = out["steps"][0]
    assert step["status"] == "failed"
    assert step["name"] == "crash"
    assert "ValueError: boom" in step["error"]


def test_nested_step_raises():
    trace = TraceRecorder()
    with trace.step("outer"):
        with pytest.raises(RuntimeError, match="while step 'outer'"):
            with trace.step("inner"):
                pass


def test_record_outputs_outside_step_raises():
    trace = TraceRecorder()
    with pytest.raises(RuntimeError, match="outside a step"):
        trace.record_outputs({"x": 1})


def test_record_llm_call_outside_step_raises():
    trace = TraceRecorder()
    with pytest.raises(RuntimeError, match="outside a step"):
        trace.record_llm_call()


def test_to_dict_while_step_active_raises():
    trace = TraceRecorder()
    cm = trace.step("forgot_to_close")
    cm.__enter__()
    with pytest.raises(RuntimeError, match="still active"):
        trace.to_dict()
    cm.__exit__(None, None, None)


def test_total_llm_calls_aggregates():
    trace = TraceRecorder()
    with trace.step("a"):
        trace.record_llm_call(3)
    with trace.step("b"):
        trace.record_llm_call(2)
    assert trace.total_llm_calls() == 5
    assert trace.to_dict()["total_llm_calls"] == 5


def test_record_llm_call_invalid_count_raises():
    trace = TraceRecorder()
    with trace.step("a"):
        with pytest.raises(ValueError, match="positive"):
            trace.record_llm_call(0)
        with pytest.raises(ValueError, match="positive"):
            trace.record_llm_call(-1)


def test_summary_clips_oversized_strings():
    trace = TraceRecorder()
    huge = "x" * 10_000
    with trace.step("big", inputs_summary={"blob": huge}):
        pass
    step = trace.to_dict()["steps"][0]
    summary_value = step["inputs_summary"]["blob"]
    assert len(summary_value) < 10_000
    assert "[+" in summary_value and "chars]" in summary_value


def test_summary_coerces_non_json_values():
    trace = TraceRecorder()

    class _Custom:
        def __repr__(self):
            return "<custom-obj>"

    with trace.step("coerce", inputs_summary={"obj": _Custom()}):
        trace.record_outputs({"nested": {"obj": _Custom(), "list": [_Custom()]}})
    step = trace.to_dict()["steps"][0]
    assert step["inputs_summary"] == {"obj": "<custom-obj>"}
    assert step["outputs_summary"]["nested"]["obj"] == "<custom-obj>"
    assert step["outputs_summary"]["nested"]["list"] == ["<custom-obj>"]


def test_empty_trace_serialises_cleanly():
    trace = TraceRecorder()
    out = trace.to_dict()
    assert out["step_count"] == 0
    assert out["steps"] == []
    assert out["total_llm_calls"] == 0
    assert out["total_duration_ms"] == 0


def test_step_name_must_be_non_empty():
    trace = TraceRecorder()
    with pytest.raises(ValueError, match="non-empty"):
        with trace.step(""):
            pass


def test_duration_ms_is_non_negative():
    trace = TraceRecorder()
    with trace.step("instant"):
        pass
    assert trace.to_dict()["steps"][0]["duration_ms"] >= 0
