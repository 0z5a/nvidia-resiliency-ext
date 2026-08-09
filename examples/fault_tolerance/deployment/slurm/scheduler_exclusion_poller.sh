#!/bin/bash
# Publish scheduler-unavailable Slurm array tasks for in-job rendezvous.
#
# Run one copy on array task 0's Node0, outside the workload container. Each
# successful pass performs one filtered sinfo query and, when unavailable nodes
# exist, one node-filtered squeue query. It maps only affected array tasks and
# atomically replaces:
#
#   ${OUTPUT_DIR}/segment_health_check.${ARRAY_JOB_ID}.state
#
# The first record is the complete consumer decision, for example:
#
#   ["7","12"]
#
# The task-level decision schema follows for audit. The node-level decision is
# retained as an empty record; per-node observations are intentionally omitted.
#
# A pass is all-or-nothing. Any Slurm error or malformed row preserves the
# previous artifact. Successful passes always replace the artifact, even when
# the compact decision is unchanged, because its mtime is the consumer's
# freshness lease.
#
# The task-ID-only decision requires an explicitly non-requeued array. A requeued
# task reuses its ID and could inherit a prior exclusion. Scope OUTPUT_DIR to the
# current array job and launch the workload with --no-requeue.
#
# Example:
#
#   OUTPUT_DIR="/shared/nvrx/${SLURM_ARRAY_JOB_ID}/scheduler-exclusions" \
#   ARRAY_JOB_ID="${SLURM_ARRAY_JOB_ID}" \
#     bash scheduler_exclusion_poller.sh &
#   SCHEDULER_EXCLUSION_POLLER_PID=$!
#   trap 'kill "${SCHEDULER_EXCLUSION_POLLER_PID}" 2>/dev/null || true' EXIT
#
# Environment:
#   ARRAY_JOB_ID          Required Slurm array parent job ID.
#   OUTPUT_DIR            Required shared output directory.
#   POLL_INTERVAL         Seconds between polls. Default: 600.
#   EXCLUSION_STATES      sinfo state filter. Default: drain,down,fail,no_respond.
#   SLURM_CMD_TIMEOUT     Timeout for sinfo and squeue. Default: 30 seconds.
#   ONESHOT               Run one pass and exit; intended for testing.

set -uo pipefail

: "${ARRAY_JOB_ID:?scheduler_exclusion_poller: set ARRAY_JOB_ID}"
: "${OUTPUT_DIR:?scheduler_exclusion_poller: set OUTPUT_DIR}"

if [[ ! "${ARRAY_JOB_ID}" =~ ^[0-9]+$ ]]; then
    printf 'scheduler_exclusion_poller: ARRAY_JOB_ID must be numeric: %s\n' \
        "${ARRAY_JOB_ID}" >&2
    exit 2
fi

POLL_INTERVAL="${POLL_INTERVAL:-600}"
EXCLUSION_STATES="${EXCLUSION_STATES:-drain,down,fail,no_respond}"
SLURM_CMD_TIMEOUT="${SLURM_CMD_TIMEOUT:-30}"
MAX_DECISION_LINE_BYTES=$((8 * 1024))
DECISION_TTL_SECONDS=$((30 * 60))
DECISION_PATH="${OUTPUT_DIR}/segment_health_check.${ARRAY_JOB_ID}.state"

if [[ ! "${POLL_INTERVAL}" =~ ^[1-9][0-9]*$ ]]; then
    printf 'scheduler_exclusion_poller: POLL_INTERVAL must be a positive integer: %s\n' \
        "${POLL_INTERVAL}" >&2
    exit 2
fi

umask 022
mkdir -p "${OUTPUT_DIR}" || exit 1
WORK="$(mktemp -d "${OUTPUT_DIR}/.segment_health_check.${ARRAY_JOB_ID}.XXXXXX")" || exit 1
trap 'rm -rf "${WORK}"' EXIT

log() {
    printf '[scheduler_exclusion_poller %s] %s\n' "$(date +'%H:%M:%S')" "$*" >&2
}

trim_whitespace() {
    local value="$1"

    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "${value}"
}

format_timestamp() {
    local epoch="$1"

    if date -u -d "@${epoch}" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null; then
        return 0
    fi
    date -u -r "${epoch}" '+%Y-%m-%dT%H:%M:%SZ'
}

write_compact_decision() {
    local tasks="$1"
    local destination="$2"
    local separator=""
    local task_id

    printf '[' >>"${destination}"
    while IFS= read -r task_id; do
        [[ -n "${task_id}" ]] || continue
        printf '%s"%s"' "${separator}" "${task_id}" >>"${destination}"
        separator=,
    done <"${tasks}"
    printf ']\n' >>"${destination}"
}

write_artifact() {
    local tasks="$1"
    local observed_at="$2"
    local valid_until="$3"
    local destination="$4"
    local separator=""
    local task_id

    : >"${destination}"
    write_compact_decision "${tasks}" "${destination}" || return 1

    printf '{"type":"decision","schema_version":1,"job_id":"%s","generated_at":"%s","scope":"array_task","excluded_array_tasks":[' \
        "${ARRAY_JOB_ID}" "${observed_at}" >>"${destination}"
    while IFS= read -r task_id; do
        [[ -n "${task_id}" ]] || continue
        printf '%s{"task_id":"%s","restart_count":0,"valid_until":"%s"}' \
            "${separator}" "${task_id}" "${valid_until}" >>"${destination}"
        separator=,
    done <"${tasks}"
    printf ']}\n' >>"${destination}"

    printf '{"type":"decision","schema_version":1,"job_id":"%s","generated_at":"%s","scope":"node","excluded_nodes":[]}\n' \
        "${ARRAY_JOB_ID}" "${observed_at}" >>"${destination}"
}

poll_once() {
    local unavailable_raw="${WORK}/unavailable.raw"
    local unavailable_staged="${WORK}/unavailable.staged"
    local unavailable="${WORK}/unavailable.nodes"
    local excluded_raw="${WORK}/excluded.raw"
    local excluded="${WORK}/excluded.tasks"
    local decision="${WORK}/decision.state"
    local previous_first="${WORK}/previous.first"
    local current_first="${WORK}/current.first"
    local taskmap
    local unavailable_filter
    local task_id
    local node
    local extra
    local observed_epoch
    local valid_epoch
    local observed_at
    local valid_until
    local affected_task_count=0
    local unavailable_count
    local excluded_count
    local decision_bytes
    local changed=1

    if ! timeout "${SLURM_CMD_TIMEOUT}" \
        sinfo --states="${EXCLUSION_STATES}" --Node --noheader --format='%N' \
        >"${unavailable_raw}"; then
        log "sinfo failed or timed out; preserving the previous decision"
        return 1
    fi

    : >"${unavailable_staged}"
    while IFS= read -r node; do
        node=$(trim_whitespace "${node}")
        [[ -n "${node}" ]] || continue
        if [[ ! "${node}" =~ ^[A-Za-z0-9._-]+$ ]]; then
            log "invalid sinfo node '${node}'; preserving the previous decision"
            return 1
        fi
        printf '%s\n' "${node}" >>"${unavailable_staged}"
    done <"${unavailable_raw}"

    if ! sort -u "${unavailable_staged}" >"${unavailable}"; then
        log "could not stage unavailable nodes; preserving the previous decision"
        return 1
    fi

    if [[ -s "${unavailable}" ]]; then
        if ! unavailable_filter=$(paste -s -d ',' "${unavailable}"); then
            log "could not build the unavailable-node filter; preserving the previous decision"
            return 1
        fi
        if ! taskmap=$(timeout "${SLURM_CMD_TIMEOUT}" \
            squeue -j "${ARRAY_JOB_ID}" -r -h -t R \
            --nodelist="${unavailable_filter}" -o '%K|'); then
            log "squeue failed or timed out; preserving the previous decision"
            return 1
        fi
    else
        taskmap=""
    fi

    : >"${excluded_raw}"
    while IFS='|' read -r task_id extra; do
        task_id=$(trim_whitespace "${task_id}")
        extra=$(trim_whitespace "${extra}")
        [[ -n "${task_id}${extra}" ]] || continue
        if [[ ! "${task_id}" =~ ^[0-9]+$ || -n "${extra}" ]]; then
            log "invalid squeue task row '${task_id}|${extra}'; preserving the previous decision"
            return 1
        fi
        affected_task_count=$((affected_task_count + 1))
        printf '%s\n' "${task_id}" >>"${excluded_raw}"
    done <<<"${taskmap}"

    if ! sort -n -u "${excluded_raw}" >"${excluded}"; then
        log "could not stage excluded tasks; preserving the previous decision"
        return 1
    fi
    if ! observed_epoch=$(date +%s) || [[ ! "${observed_epoch}" =~ ^[0-9]+$ ]]; then
        log "could not read the current time; preserving the previous decision"
        return 1
    fi
    valid_epoch=$((observed_epoch + DECISION_TTL_SECONDS))
    if ! observed_at=$(format_timestamp "${observed_epoch}") \
        || ! valid_until=$(format_timestamp "${valid_epoch}"); then
        log "could not format artifact timestamps; preserving the previous decision"
        return 1
    fi
    if ! write_artifact "${excluded}" "${observed_at}" "${valid_until}" \
        "${decision}"; then
        log "could not stage the decision artifact; preserving the previous decision"
        return 1
    fi
    if ! head -n 1 "${decision}" >"${current_first}" \
        || ! decision_bytes=$(wc -c <"${current_first}"); then
        log "could not measure the decision artifact; preserving the previous decision"
        return 1
    fi
    if (( decision_bytes - 1 > MAX_DECISION_LINE_BYTES )); then
        log "decision exceeds ${MAX_DECISION_LINE_BYTES} bytes; preserving the previous decision"
        return 1
    fi

    if [[ -f "${DECISION_PATH}" ]]; then
        if head -n 1 "${DECISION_PATH}" >"${previous_first}" \
            && cmp -s "${current_first}" "${previous_first}"; then
            changed=0
        fi
    fi
    if ! mv -f "${decision}" "${DECISION_PATH}"; then
        log "could not publish the decision artifact; preserving the previous decision"
        return 1
    fi

    unavailable_count=$(wc -l <"${unavailable}")
    excluded_count=$(wc -l <"${excluded}")
    if (( changed )); then
        log "published affected_tasks=${affected_task_count} unavailable_nodes=${unavailable_count} excluded_tasks=${excluded_count}"
    elif [[ -n "${FIRST:-}" ]]; then
        log "refreshed affected_tasks=${affected_task_count} unavailable_nodes=${unavailable_count} excluded_tasks=${excluded_count}"
    fi
    FIRST=
    return 0
}

log "start array=${ARRAY_JOB_ID} output_dir=${OUTPUT_DIR} interval=${POLL_INTERVAL}s states=${EXCLUSION_STATES}"
FIRST=1
while :; do
    if poll_once; then
        poll_status=0
    else
        poll_status=$?
    fi
    if [[ -n "${ONESHOT:-}" ]]; then
        exit "${poll_status}"
    fi
    sleep "${POLL_INTERVAL}"
done
