# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Behavioral tests for the single-source L1 evidence-tool contracts."""

import json

from nvidia_resiliency_ext.attribution.restart_agent.infrastructure.log_source import LogSnapshot
from nvidia_resiliency_ext.attribution.restart_agent.l0 import build_l0_bundle
from nvidia_resiliency_ext.attribution.restart_agent.l0.decision import build_decision_evidence
from nvidia_resiliency_ext.attribution.restart_agent.l0.projection import build_l0_model_facing_view
from nvidia_resiliency_ext.attribution.restart_agent.l1 import L1EvidenceResult
from nvidia_resiliency_ext.attribution.restart_agent.l1.openai_compatible import (
    LlmConfig,
    _tool_loop_profile,
    _tool_schemas,
)
from nvidia_resiliency_ext.attribution.restart_agent.l1.tool_contracts import (
    DEFAULT_ADVERTISED_TOOLS,
    L1_TOOL_CONTRACTS,
    TOOL_RESULT_SCHEMA_VERSION,
    advertised_tool_schemas,
    execute_tool_request,
)
from nvidia_resiliency_ext.attribution.restart_agent.l1.tools import LogTools
from nvidia_resiliency_ext.attribution.restart_agent.l2.grounding import (
    model_visible_line_numbers,
    model_visible_line_texts,
)
from nvidia_resiliency_ext.attribution.restart_agent.models import L0Bundle


def _tools(lines: tuple[str, ...] | None = None) -> LogTools:
    lines = lines or (
        "iteration 1 completed",
        "RuntimeError: observed failure",
        "scheduler cancelled step",
    )
    bundle = L0Bundle(
        log_path="/not/read.log",
        byte_size=sum(len(line) + 1 for line in lines),
        line_count=len(lines),
    )
    return LogTools(
        bundle,
        LogSnapshot(path=bundle.log_path, lines=lines, byte_size=bundle.byte_size),
    )


def _execute(name: str, arguments, *, advertised=None):
    return execute_tool_request(
        _tools(),
        name=name,
        raw_arguments=arguments,
        advertised_tools=advertised or (*DEFAULT_ADVERTISED_TOOLS, "get_evidence_objects"),
    )


def test_advertised_schemas_come_from_the_executable_contract_registry():
    schemas = advertised_tool_schemas(DEFAULT_ADVERTISED_TOOLS)

    assert [item["function"]["name"] for item in schemas] == list(DEFAULT_ADVERTISED_TOOLS)
    grep_parameters = schemas[1]["function"]["parameters"]
    assert grep_parameters["properties"]["max_matches"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 200,
        "default": 50,
    }
    assert set(L1_TOOL_CONTRACTS) == {
        "overview",
        "grep_log",
        "read_window",
        "get_evidence_objects",
    }


def test_zero_tool_rounds_resolves_to_one_tools_disabled_model_turn():
    config = LlmConfig(tools_enabled=True, max_tool_rounds=0)

    assert config.resolved_advertised_tools() == ()
    assert config.tools_active() is False
    assert _tool_schemas(config) == []
    assert _tool_loop_profile(config) == {
        "tools_enabled": False,
        "advertised_tools": [],
        "max_tool_rounds": 0,
        "max_model_turns": 1,
        "meaning": "single tools-disabled model turn",
    }


def test_negative_tool_rounds_are_rejected_at_l1_config_construction():
    try:
        LlmConfig(max_tool_rounds=-1)
    except ValueError as exc:
        assert str(exc) == "max_tool_rounds must not be negative"
    else:
        raise AssertionError("negative tool rounds must be rejected")


def test_successful_tool_result_uses_the_common_envelope():
    result, arguments, unsupported = _execute(
        "grep_log",
        json.dumps({"pattern": "RuntimeError"}),
    )

    assert result["schema_version"] == TOOL_RESULT_SCHEMA_VERSION
    assert result["tool"] == "grep_log"
    assert result["status"] == "ok"
    assert result["error"] is None
    assert result["data"]["matches"] == [{"line": 2, "text": "RuntimeError: observed failure"}]
    assert result["limits"]["max_matches"] == 50
    assert arguments == {
        "pattern": "RuntimeError",
        "ignore_case": True,
        "max_matches": 50,
    }
    assert unsupported is False


def test_explicit_grep_limit_above_the_default_is_honored():
    lines = tuple(f"RuntimeError: failure {index}" for index in range(75))

    result, arguments, unsupported = execute_tool_request(
        _tools(lines),
        name="grep_log",
        raw_arguments={"pattern": "RuntimeError", "max_matches": 60},
        advertised_tools=DEFAULT_ADVERTISED_TOOLS,
    )

    assert result["status"] == "ok"
    assert len(result["data"]["matches"]) == 60
    assert result["data"]["total_matches"] == 75
    assert result["data"]["truncated"] is True
    assert result["limits"]["max_matches"] == 60
    assert arguments["max_matches"] == 60
    assert unsupported is False


def test_symmetric_read_window_limit_includes_the_center_line():
    lines = tuple(f"line {index}" for index in range(1, 302))

    result, arguments, unsupported = execute_tool_request(
        _tools(lines),
        name="read_window",
        raw_arguments={"center_line": 151, "before": 120, "after": 120},
        advertised_tools=DEFAULT_ADVERTISED_TOOLS,
    )

    assert result["status"] == "ok"
    assert result["data"]["start_line"] == 31
    assert result["data"]["end_line"] == 271
    assert len(result["data"]["lines"]) == 241
    assert result["limits"]["max_lines"] == 241
    assert arguments == {"center_line": 151, "before": 120, "after": 120}
    assert unsupported is False


def test_malformed_json_and_type_coercion_are_rejected_with_closed_codes():
    malformed, _, _ = _execute("grep_log", "{")
    wrong_type, _, _ = _execute(
        "grep_log",
        {"pattern": "failure", "ignore_case": "false"},
    )

    assert malformed["status"] == "error"
    assert malformed["error"]["code"] == "malformed_arguments_json"
    assert wrong_type["error"] == {
        "code": "invalid_arguments",
        "field": "ignore_case",
        "message": "ignore_case must be a boolean.",
    }


def test_invalid_regex_and_out_of_range_line_are_rejected_before_execution():
    invalid_regex, _, _ = _execute("grep_log", {"pattern": "["})
    out_of_range, _, _ = _execute("read_window", {"center_line": 4})

    assert invalid_regex["error"]["code"] == "invalid_regex"
    assert out_of_range["error"]["code"] == "line_out_of_range"
    assert out_of_range["error"]["field"] == "center_line"


def test_every_registry_entry_enforces_its_required_success_shape():
    calls = (
        ("overview", {}),
        ("grep_log", {"pattern": "failure"}),
        ("read_window", {"center_line": 2}),
        ("get_evidence_objects", {"refs": ["missing"]}),
    )

    for name, arguments in calls:
        result, _, _ = _execute(name, arguments)
        assert set(result) == {
            "schema_version",
            "tool",
            "status",
            "data",
            "error",
            "truncated",
            "limits",
        }
        assert result["schema_version"] == TOOL_RESULT_SCHEMA_VERSION
        assert result["tool"] == name
        assert result["status"] == "ok", (name, result)
        assert set(L1_TOOL_CONTRACTS[name].result_required_fields).issubset(result["data"])


def test_unknown_arguments_are_rejected_instead_of_ignored():
    result, _, _ = _execute("overview", {"surprise": True})

    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["field"] == "surprise"


def test_unadvertised_tool_uses_the_same_error_envelope():
    result, _, unsupported = _execute(
        "get_evidence_objects",
        {"refs": ["missing"]},
        advertised=DEFAULT_ADVERTISED_TOOLS,
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "tool_not_advertised"
    assert unsupported is True


def test_tool_name_rejection_uses_advertisement_first_precedence():
    unadvertised, _, unsupported = _execute(
        "invented_tool",
        {},
        advertised=DEFAULT_ADVERTISED_TOOLS,
    )
    advertised_but_unimplemented, _, advertised_unsupported = _execute(
        "invented_tool",
        {},
        advertised=(*DEFAULT_ADVERTISED_TOOLS, "invented_tool"),
    )

    assert unadvertised["error"]["code"] == "tool_not_advertised"
    assert unsupported is True
    assert advertised_but_unimplemented["error"]["code"] == "tool_not_implemented"
    assert advertised_unsupported is True


def test_failed_tool_results_do_not_expand_l2_model_visibility(tmp_path):
    log_path = tmp_path / "attempt.log"
    log_path.write_text("iteration 1 completed\nRuntimeError: failure\n", encoding="utf-8")
    bundle = build_l0_bundle(str(log_path))
    model_view = build_l0_model_facing_view(bundle, build_decision_evidence(bundle))
    result = L1EvidenceResult(
        semantic_payload=None,
        model="test-model",
        transcript_events=(
            {
                "event_type": "tool_result",
                "result": {
                    "schema_version": TOOL_RESULT_SCHEMA_VERSION,
                    "tool": "read_window",
                    "status": "error",
                    "data": None,
                    "error": {
                        "code": "line_out_of_range",
                        "field": "center_line",
                        "message": "line 999999 is unavailable",
                    },
                    "truncated": False,
                    "limits": {},
                },
            },
            {
                "event_type": "tool_result",
                "result": {
                    "schema_version": TOOL_RESULT_SCHEMA_VERSION,
                    "tool": "read_window",
                    "status": "ok",
                    "data": {
                        "start_line": 444,
                        "end_line": 444,
                        "lines": [{"line": 444, "text": "visible tool evidence"}],
                        "truncated": False,
                    },
                    "error": None,
                    "truncated": False,
                    "limits": {"max_lines": 241},
                },
            },
        ),
    )

    visible_lines = model_visible_line_numbers(model_view, result)
    visible_texts = model_visible_line_texts(model_view, result)
    assert 999999 not in visible_lines
    assert 999999 not in visible_texts
    assert 444 in visible_lines
    assert visible_texts[444] == {"visible tool evidence"}
