# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).parents[3]
    / "examples/fault_tolerance/deployment/slurm/scheduler_exclusion_poller.sh"
)


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def poller_environment(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sinfo_output = tmp_path / "sinfo.out"
    squeue_output = tmp_path / "squeue.out"
    sinfo_output.write_text("", encoding="utf-8")
    squeue_output.write_text("7|\n", encoding="utf-8")

    _write_executable(
        fake_bin / "timeout",
        "#!/bin/bash\nshift\nexec \"$@\"\n",
    )
    _write_executable(
        fake_bin / "sinfo",
        """#!/bin/bash
if [[ -n "${FAKE_SINFO_FAIL:-}" ]]; then echo "fake sinfo error" >&2; exit 1; fi
printf '%s\n' "$*" >"${FAKE_SINFO_ARGS}"
cat "${FAKE_SINFO_OUTPUT}"
""",
    )
    _write_executable(
        fake_bin / "squeue",
        """#!/bin/bash
if [[ -n "${FAKE_SQUEUE_FAIL:-}" ]]; then echo "fake squeue error" >&2; exit 1; fi
printf '%s\n' "$*" >"${FAKE_SQUEUE_ARGS}"
cat "${FAKE_SQUEUE_OUTPUT}"
""",
    )
    output_dir = tmp_path / "decisions"
    environment = os.environ.copy()
    environment.update(
        {
            "ARRAY_JOB_ID": "123",
            "FAKE_SINFO_ARGS": str(tmp_path / "sinfo.args"),
            "FAKE_SINFO_OUTPUT": str(sinfo_output),
            "FAKE_SQUEUE_ARGS": str(tmp_path / "squeue.args"),
            "FAKE_SQUEUE_OUTPUT": str(squeue_output),
            "ONESHOT": "1",
            "OUTPUT_DIR": str(output_dir),
            "PATH": f"{fake_bin}:{environment['PATH']}",
        }
    )
    return environment, output_dir, sinfo_output, squeue_output


def _run_poller(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(_SCRIPT)],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def _decision_path(output_dir: Path) -> Path:
    return output_dir / "segment_health_check.123.state"


def _read_records(path: Path) -> list[object]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_publishes_sorted_compact_array_task_decision(poller_environment):
    environment, output_dir, sinfo_output, squeue_output = poller_environment
    sinfo_output.write_text("node04\nnode02\n", encoding="utf-8")
    squeue_output.write_text("12|\n7|\n", encoding="utf-8")

    result = _run_poller(environment)

    assert result.returncode == 0, result.stderr
    records = _read_records(_decision_path(output_dir))
    assert records[0] == ["7", "12"]
    assert records[1]["type"] == "decision"
    assert records[1]["schema_version"] == 1
    assert records[1]["job_id"] == "123"
    assert records[1]["scope"] == "array_task"
    assert [entry["task_id"] for entry in records[1]["excluded_array_tasks"]] == [
        "7",
        "12",
    ]
    assert {entry["restart_count"] for entry in records[1]["excluded_array_tasks"]} == {0}
    assert records[2]["scope"] == "node"
    assert records[2]["excluded_nodes"] == []
    assert len(records) == 3
    observed_at = datetime.fromisoformat(records[1]["generated_at"].replace("Z", "+00:00"))
    valid_until = datetime.fromisoformat(
        records[1]["excluded_array_tasks"][0]["valid_until"].replace("Z", "+00:00")
    )
    assert (valid_until - observed_at).total_seconds() == 30 * 60

    sinfo_args = Path(environment["FAKE_SINFO_ARGS"]).read_text(encoding="utf-8")
    assert "--states=drain,down,fail,no_respond" in sinfo_args
    assert "--format=%N" in sinfo_args
    squeue_args = Path(environment["FAKE_SQUEUE_ARGS"]).read_text(encoding="utf-8")
    assert "--nodelist=node02,node04" in squeue_args
    assert "-o %K|" in squeue_args


def test_trims_scheduler_field_padding(poller_environment):
    environment, output_dir, sinfo_output, squeue_output = poller_environment
    sinfo_output.write_text("  node04  \n\tnode02\t\n", encoding="utf-8")
    squeue_output.write_text(" 12 | \n\t7\t|\t\n", encoding="utf-8")

    result = _run_poller(environment)

    assert result.returncode == 0, result.stderr
    assert _read_records(_decision_path(output_dir))[0] == ["7", "12"]
    squeue_args = Path(environment["FAKE_SQUEUE_ARGS"]).read_text(encoding="utf-8")
    assert "--nodelist=node02,node04" in squeue_args


def test_successful_clean_poll_publishes_empty_decision(poller_environment):
    environment, output_dir, _, _ = poller_environment

    result = _run_poller(environment)

    assert result.returncode == 0, result.stderr
    records = _read_records(_decision_path(output_dir))
    assert records[0] == []
    assert records[1]["excluded_array_tasks"] == []
    assert records[2]["excluded_nodes"] == []
    assert len(records) == 3
    assert not Path(environment["FAKE_SQUEUE_ARGS"]).exists()


def test_unavailable_nodes_outside_job_publish_empty_decision(poller_environment):
    environment, output_dir, sinfo_output, squeue_output = poller_environment
    sinfo_output.write_text("other-node\n", encoding="utf-8")
    squeue_output.write_text("", encoding="utf-8")

    result = _run_poller(environment)

    assert result.returncode == 0, result.stderr
    assert _read_records(_decision_path(output_dir))[0] == []
    squeue_args = Path(environment["FAKE_SQUEUE_ARGS"]).read_text(encoding="utf-8")
    assert "--nodelist=other-node" in squeue_args


def test_target_scale_decision_fits_consumer_bound(poller_environment):
    environment, output_dir, sinfo_output, squeue_output = poller_environment
    first_task_id = 1_000_000_000
    task_ids = [str(first_task_id + offset) for offset in range(313)]
    sinfo_output.write_text(
        "".join(f"node{offset}\n" for offset in range(len(task_ids))),
        encoding="utf-8",
    )
    squeue_output.write_text(
        "".join(f"{task_id}|\n" for task_id in task_ids),
        encoding="utf-8",
    )

    result = _run_poller(environment)

    assert result.returncode == 0, result.stderr
    decision = _decision_path(output_dir)
    first_line = decision.read_bytes().splitlines()[0]
    assert json.loads(first_line) == task_ids
    assert len(first_line) < 8 * 1024


def test_successful_unchanged_poll_refreshes_artifact_mtime(poller_environment):
    environment, output_dir, sinfo_output, _ = poller_environment
    sinfo_output.write_text("node01\n", encoding="utf-8")
    first = _run_poller(environment)
    assert first.returncode == 0, first.stderr
    decision = _decision_path(output_dir)
    os.utime(decision, (1, 1))

    second = _run_poller(environment)

    assert second.returncode == 0, second.stderr
    assert _read_records(decision)[0] == ["7"]
    assert decision.stat().st_mtime > 1


@pytest.mark.parametrize("failure", ["sinfo", "squeue"])
def test_failed_pass_preserves_previous_decision(poller_environment, failure):
    environment, output_dir, sinfo_output, _ = poller_environment
    output_dir.mkdir()
    decision = _decision_path(output_dir)
    decision.write_text('["7"]\n', encoding="utf-8")
    os.utime(decision, (1, 1))

    if failure == "sinfo":
        environment["FAKE_SINFO_FAIL"] = "1"
    else:
        sinfo_output.write_text("node01\n", encoding="utf-8")
        environment["FAKE_SQUEUE_FAIL"] = "1"

    result = _run_poller(environment)

    assert result.returncode != 0
    assert f"fake {failure} error" in result.stderr
    assert decision.read_text(encoding="utf-8") == '["7"]\n'
    assert decision.stat().st_mtime == 1


def test_malformed_task_mapping_preserves_previous_decision(poller_environment):
    environment, output_dir, sinfo_output, squeue_output = poller_environment
    output_dir.mkdir()
    decision = _decision_path(output_dir)
    decision.write_text('["7"]\n', encoding="utf-8")
    sinfo_output.write_text("node01\n", encoding="utf-8")
    squeue_output.write_text("not-a-task|\n", encoding="utf-8")

    result = _run_poller(environment)

    assert result.returncode != 0
    assert decision.read_text(encoding="utf-8") == '["7"]\n'


def test_malformed_scheduler_node_preserves_previous_decision(poller_environment):
    environment, output_dir, sinfo_output, _ = poller_environment
    output_dir.mkdir()
    decision = _decision_path(output_dir)
    decision.write_text('["7"]\n', encoding="utf-8")
    sinfo_output.write_text("node01|unexpected\n", encoding="utf-8")

    result = _run_poller(environment)

    assert result.returncode != 0
    assert decision.read_text(encoding="utf-8") == '["7"]\n'


def test_array_job_id_must_be_numeric(poller_environment):
    environment, output_dir, _, _ = poller_environment
    environment["ARRAY_JOB_ID"] = "../../wrong"

    result = _run_poller(environment)

    assert result.returncode == 2
    assert not output_dir.exists()


@pytest.mark.parametrize("value", ["0", "invalid"])
def test_poll_interval_must_be_a_positive_integer(poller_environment, value):
    environment, output_dir, _, _ = poller_environment
    environment["POLL_INTERVAL"] = value

    result = _run_poller(environment)

    assert result.returncode == 2
    assert f"POLL_INTERVAL must be a positive integer: {value}" in result.stderr
    assert not output_dir.exists()
