# Segment Health Check: FT Consumer Design

## Purpose

NVRx consumes a shared segment health decision before joining rendezvous.
The restart path performs filesystem I/O only; it does not query Slurm or call
an external service. The
[integration guide](../source/fault_tolerance/integration/segment_health_check.rst)
owns the user-facing configuration, artifact contract, and supported launch
shape.

## Internal Flow

```text
FtRendezvousBarrierHandler initialization
  -> get_segment_health_check()
       -> ineligible launcher: no check installed
       `-> eligible allocation-unit process 0: SegmentHealthCheck(directory, job_id, task_id)

pre_join_hook
  -> ensure_node_is_healthy()
       -> SegmentHealthCheck._perform_health_check()
       -> existing local node checks
       `-> _run_health_check() converts failure to UnhealthyNodeException
            -> array path marks replacement group unhealthy
            `-> regular-job path exits the workload step
```

### Installation Guard

`get_segment_health_check()` installs the check only on the launcher with
`SLURM_PROCID=0`. Arrays use `SLURM_ARRAY_JOB_ID` and `SLURM_ARRAY_TASK_ID`;
regular jobs use `SLURM_JOB_ID` as both the artifact scope and exclusion token.
Missing job identity leaves the check uninstalled; other launchers return
without logging. Successful installation emits one INFO record.

### Decision Evaluation

The concrete `SegmentHealthCheck` in
`fault_tolerance/segment_health_check.py` owns the configured directory and
Slurm job/task IDs. Its `_perform_health_check()`:

1. derives `segment_health_check.<job_id>.state` from the configured directory;
2. reads the newline-terminated control record with a bounded `readline()` and
   an 8 KiB upper bound;
3. validates the exact compact allocation-ID array; and
4. performs an exact quoted byte-token match, so task `7` does not match task
   `17`.

At the target scale of 5,000 nodes with 16 nodes per array task, the record has
at most 313 task IDs; even ten-digit IDs occupy about 4 KiB.

Missing, malformed, oversized, or unreadable input fails open. A missing file
is quiet because the producer may not be running; invalid or unreadable input
is logged as a warning.

### Rendezvous Handoff

A matching allocation ID returns unhealthy through `_run_health_check()`, which
raises `UnhealthyNodeException`. For arrays, the existing pre-join exception
path uses `SLURM_ARRAY_TASK_ID` as the replacement-group token and adds it to
`unhealthy_replacement_groups_<round>`. A regular job has no replacement-group
token; process 0 exits nonzero and `srun --kill-on-bad-exit=1` terminates its
workload step. Segment Health Check adds no host-side selection path.

## Implementation Map

| Area | Responsibility |
| --- | --- |
| `fault_tolerance/segment_health_check.py` | Identify eligible process-0 launchers and implement `SegmentHealthCheck`. |
| `fault_tolerance/config.py` and launcher plumbing | Validate and pass the decision directory into rendezvous settings. |
| `fault_tolerance/ft_rendezvous_barrier.py` | Install the check and reuse the existing health-check exception path. |
| `tests/fault_tolerance/unit/` | Cover installation, bounded reads, matching, failure handling, and rendezvous propagation. |

## Verification

Environment-specific E2E scenarios, commands, and result analysis live with
the deployment validation harness rather than this product design.
