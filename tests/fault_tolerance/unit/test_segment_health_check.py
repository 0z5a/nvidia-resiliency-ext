# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import os
from unittest.mock import patch

import pytest

from nvidia_resiliency_ext.fault_tolerance.segment_health_check import (
    SegmentHealthCheck,
    get_segment_health_check,
)


def _environment(**overrides):
    values = {
        "SLURM_ARRAY_JOB_ID": "123",
        "SLURM_ARRAY_TASK_ID": "7",
        "SLURM_NODEID": "0",
        "SLURM_PROCID": "0",
    }
    values.update(overrides)
    return values


def _write_decision(tmp_path, tasks=("7",), *, modified_at=None):
    path = tmp_path / "segment_health_check.123.state"
    path.write_text(json.dumps(list(tasks), separators=(",", ":")) + "\n", encoding="utf-8")
    if modified_at is not None:
        os.utime(path, (modified_at, modified_at))
    return path


def _is_segment_healthy(directory, **kwargs):
    task_id = kwargs.pop("task_id", "7")
    assert not kwargs
    return SegmentHealthCheck(str(directory), "123", task_id)()


def test_segment_health_check_is_installed_only_on_array_task_process_zero():
    with patch.dict(os.environ, _environment(SLURM_JOB_ID="123_7"), clear=True):
        check = get_segment_health_check("/shared/nvrx")
    assert check is not None
    assert check.job_id == "123"
    assert check.task_id == "7"

    with patch.dict(
        os.environ,
        _environment(SLURM_NODEID="0", SLURM_PROCID="1"),
        clear=True,
    ):
        assert get_segment_health_check("/shared/nvrx") is None

    with patch.dict(
        os.environ,
        _environment(SLURM_NODEID="1", SLURM_PROCID="0"),
        clear=True,
    ):
        assert get_segment_health_check("/shared/nvrx") is not None

    env = _environment()
    del env["SLURM_PROCID"]
    with patch.dict(os.environ, env, clear=True):
        assert get_segment_health_check("/shared/nvrx") is None


def test_segment_health_check_is_installed_for_regular_job_process_zero(tmp_path):
    path = tmp_path / "segment_health_check.456.state"
    path.write_text('["456"]\n', encoding="utf-8")

    with patch.dict(
        os.environ,
        {"SLURM_JOB_ID": "456", "SLURM_PROCID": "0"},
        clear=True,
    ):
        check = get_segment_health_check(str(tmp_path))

    assert check is not None
    assert check.job_id == "456"
    assert check.task_id == "456"
    assert not check()

    path.write_text('[]\n', encoding="utf-8")
    assert check()


def test_segment_health_check_is_not_installed_without_job_metadata():
    env = _environment()
    del env["SLURM_ARRAY_JOB_ID"]
    del env["SLURM_ARRAY_TASK_ID"]

    with patch.dict(os.environ, env, clear=True):
        assert get_segment_health_check("/shared/nvrx") is None


def test_matching_task_is_excluded(tmp_path):
    _write_decision(tmp_path)

    assert not _is_segment_healthy(tmp_path)


def test_only_first_decision_record_is_consumed(tmp_path):
    path = _write_decision(tmp_path)
    with path.open("ab") as stream:
        stream.write(b"x" * (128 * 1024))

    assert not _is_segment_healthy(tmp_path)


def test_expected_scale_decision_record_is_supported(tmp_path):
    first_task_id = 1_000_000_000
    path = _write_decision(
        tmp_path,
        tasks=(str(first_task_id + offset) for offset in range(313)),
    )

    assert path.stat().st_size < 8 * 1024
    assert not _is_segment_healthy(tmp_path, task_id=str(first_task_id + 312))


def test_quoted_task_id_does_not_match_a_longer_id(tmp_path):
    _write_decision(tmp_path, tasks=("17",))

    assert _is_segment_healthy(tmp_path, task_id="7")


@pytest.mark.parametrize(
    "record",
    [
        '["7", "12"]\n',
        ' [ "7" , "12" ] \n',
        '["7","12"]\r\n',
        '["7","12"] \n',
    ],
)
def test_noncompact_json_encodings_fail_open(tmp_path, record):
    path = tmp_path / "segment_health_check.123.state"
    path.write_text(record, encoding="utf-8", newline="")

    assert _is_segment_healthy(tmp_path)


@pytest.mark.parametrize(
    ("tasks", "task_id"),
    [
        (("7",), "8"),
        ((), "7"),
    ],
)
def test_clean_decision_is_healthy(tmp_path, tasks, task_id):
    _write_decision(tmp_path, tasks=tasks)

    assert _is_segment_healthy(tmp_path, task_id=task_id)


def test_old_decision_remains_authoritative(tmp_path):
    _write_decision(tmp_path, modified_at=100.0)

    assert not _is_segment_healthy(tmp_path)


@pytest.mark.parametrize(
    "writer",
    [
        lambda path: path.write_text("not-json\n", encoding="utf-8"),
        lambda path: path.write_text(json.dumps({"type": "decision"}) + "\n", encoding="utf-8"),
        lambda path: path.write_text('["7",garbage]\n', encoding="utf-8"),
        lambda path: path.write_text('[7,12]\n', encoding="utf-8"),
        lambda path: path.write_text('["7",null]\n', encoding="utf-8"),
        lambda path: path.write_bytes(b"{" + b"x" * (8 * 1024)),
    ],
)
def test_malformed_decision_fails_open(tmp_path, writer):
    path = tmp_path / "segment_health_check.123.state"
    writer(path)

    assert _is_segment_healthy(tmp_path)


def test_missing_decision_fails_open_quietly(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert _is_segment_healthy(tmp_path)
    assert not caplog.text


def test_io_failure_is_observable_and_fails_open(tmp_path, caplog):
    with (
        caplog.at_level(logging.INFO),
        patch(
            "nvidia_resiliency_ext.fault_tolerance.segment_health_check.Path.open",
            side_effect=OSError("storage unavailable"),
        ),
    ):
        assert _is_segment_healthy(tmp_path)

    assert "Ignoring segment health decision: storage unavailable" in caplog.text
