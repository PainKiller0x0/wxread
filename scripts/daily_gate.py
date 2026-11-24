"""Decide whether a scheduled wxread run still needs to read today.

GitHub Actions may delay or drop a scheduled workflow without creating a run.
The deploy workflow therefore has several Asia/Shanghai cron slots. This gate
lets the first successful "Run deployment script" step of a Shanghai calendar
day do the read, and tells later slots to skip.

workflow_dispatch does not call this script. A manual run always reads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEPLOY_STEP = "Run deployment script"
WORKFLOW_FILE = "deploy.yml"
# A run can start before Shanghai midnight and finish the read after it.
SEARCH_LOOKBACK = timedelta(hours=12)
SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Decision:
    should_read: bool
    reason: str


def parse_github_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {value}")
    return parsed.astimezone(timezone.utc)


def shanghai_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(SHANGHAI)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def in_search_window(created_at: datetime, day_start: datetime, day_end: datetime) -> bool:
    return (day_start - SEARCH_LOOKBACK) <= created_at.astimezone(SHANGHAI) < day_end


def deployment_succeeded(jobs: list[dict], day_start: datetime, day_end: datetime) -> int | None:
    """Return the run id whose read step completed in [day_start, day_end)."""
    for job in jobs:
        for step in job.get("steps") or []:
            if step.get("name") != DEPLOY_STEP or step.get("conclusion") != "success":
                continue
            completed_at = step.get("completed_at")
            if not completed_at:
                continue
            completed = parse_github_time(completed_at).astimezone(SHANGHAI)
            if day_start <= completed < day_end:
                run_id = job.get("run_id")
                return int(run_id) if run_id is not None else -1
    return None


def decide(
    runs: list[dict],
    jobs_by_run_id: dict,
    *,
    now: datetime,
    current_run_id: int | None,
) -> Decision:
    day_start, day_end = shanghai_day_bounds(now)
    day = day_start.date().isoformat()
    for run in runs:
        run_id = int(run["id"])
        if current_run_id is not None and run_id == int(current_run_id):
            continue
        created_at = parse_github_time(run["created_at"])
        if not in_search_window(created_at, day_start, day_end):
            continue
        jobs = jobs_by_run_id.get(run_id)
        if jobs is None:
            jobs = jobs_by_run_id.get(str(run_id))
        if not jobs:
            continue
        if deployment_succeeded(jobs, day_start, day_end) is not None:
            return Decision(
                False,
                f"run {run_id} already completed {DEPLOY_STEP} on {day} Asia/Shanghai; skipping",
            )
    return Decision(True, f"no completed read yet for {day} Asia/Shanghai")


def _utc_now() -> datetime:
    override = os.environ.get("DAILY_GATE_NOW")
    if override:
        return parse_github_time(override)
    return datetime.now(timezone.utc)


def _current_run_id() -> int | None:
    raw = os.environ.get("GITHUB_RUN_ID")
    if not raw:
        return None
    return int(raw)


def _gh_json(path: str) -> dict:
    completed = subprocess.run(
        ["gh", "api", path],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"gh api failed with status {completed.returncode}")
    return json.loads(completed.stdout)


def evaluate_live() -> Decision:
    repo = os.environ["GITHUB_REPOSITORY"]
    now = _utc_now()
    current_run_id = _current_run_id()
    day_start, day_end = shanghai_day_bounds(now)
    payload = _gh_json(
        f"repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs?per_page=30&exclude_pull_requests=true"
    )
    runs = payload.get("workflow_runs") or []
    jobs_by_run_id: dict[int, list] = {}
    for run in runs:
        run_id = int(run["id"])
        if current_run_id is not None and run_id == current_run_id:
            continue
        created_at = parse_github_time(run["created_at"])
        if not in_search_window(created_at, day_start, day_end):
            continue
        job_payload = _gh_json(
            f"repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100"
        )
        jobs_by_run_id[run_id] = job_payload.get("jobs") or []
    return decide(runs, jobs_by_run_id, now=now, current_run_id=current_run_id)


def evaluate_fixture(raw: str) -> Decision:
    payload = json.loads(raw)
    jobs_by_run_id = {int(key): value for key, value in payload.get("jobs_by_run_id", {}).items()}
    return decide(
        payload.get("runs") or [],
        jobs_by_run_id,
        now=parse_github_time(payload["now"]),
        current_run_id=payload.get("current_run_id"),
    )


def write_output(should_read: bool) -> None:
    line = f"read={'true' if should_read else 'false'}\n"
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(line)
    sys.stdout.write(line)


def main() -> int:
    fixture = os.environ.get("DAILY_GATE_FIXTURE")
    if fixture:
        decision = evaluate_fixture(fixture)
        write_output(decision.should_read)
        print(decision.reason)
        return 0

    should_read = True
    try:
        decision = evaluate_live()
        should_read = decision.should_read
        print(decision.reason)
    except Exception:
        # Fail open: a gate outage must not drop the only read of the day.
        # The exception text is omitted because API errors can echo request URLs.
        print(
            "daily gate could not check earlier runs; proceeding with read",
            file=sys.stderr,
        )
    write_output(should_read)
    return 0


if __name__ == "__main__":
    sys.exit(main())
