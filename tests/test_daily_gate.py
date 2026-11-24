import json
import os
import re
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daily_gate  # noqa: E402


def jobs(run_id, completed_at, conclusion="success", step_name=daily_gate.DEPLOY_STEP):
    return [
        {
            "run_id": run_id,
            "name": "deploy",
            "steps": [
                {
                    "name": step_name,
                    "conclusion": conclusion,
                    "completed_at": completed_at,
                }
            ],
        }
    ]


class DailyGateTests(unittest.TestCase):
    def decide(self, runs, jobs_by_run_id, now, current_run_id=None):
        return daily_gate.decide(
            runs,
            jobs_by_run_id,
            now=datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc),
            current_run_id=current_run_id,
        )

    def test_skips_after_successful_read_on_the_same_shanghai_date(self):
        # 2026-11-24 01:40 CST completed; checked later that morning.
        decision = self.decide(
            [{"id": 1, "created_at": "2026-11-23T17:20:00Z"}],
            {1: jobs(1, "2026-11-23T17:40:00Z")},
            "2026-11-23T21:30:00Z",
        )
        self.assertFalse(decision.should_read)
        self.assertIn("2026-11-24", decision.reason)

    def test_reads_when_the_only_success_was_yesterday_in_shanghai(self):
        # Run 206 finished 2026-11-23 07:26 CST, which is the previous date
        # relative to the missed 2026-11-24 morning.
        decision = self.decide(
            [{"id": 45077519928, "created_at": "2026-11-22T22:24:11Z"}],
            {45077519928: jobs(45077519928, "2026-11-22T23:26:32Z")},
            "2026-11-24T01:19:00Z",
        )
        self.assertTrue(decision.should_read)

    def test_reads_again_when_today_attempt_failed(self):
        for conclusion in ("failure", "cancelled", "timed_out", "skipped", None):
            decision = self.decide(
                [{"id": 7, "created_at": "2026-11-23T17:20:00Z"}],
                {7: jobs(7, "2026-11-23T17:40:00Z", conclusion=conclusion)},
                "2026-11-23T21:30:00Z",
            )
            self.assertTrue(decision.should_read, conclusion)

    def test_late_same_day_success_suppresses_the_next_slot(self):
        # Historical starts landed around 08:44 Asia/Shanghai. A 09:29 slot
        # must not read again once that attempt's script step has succeeded.
        decision = self.decide(
            [{"id": 8, "created_at": "2026-11-24T00:44:00Z"}],
            {8: jobs(8, "2026-11-24T01:46:00Z")},
            "2026-11-24T01:50:00Z",
        )
        self.assertFalse(decision.should_read)

    def test_cross_midnight_delay_counts_on_the_finish_date(self):
        # Created 20:00 Asia/Shanghai on Nov 23, script finished 00:30 on Nov 24.
        decision = self.decide(
            [{"id": 4, "created_at": "2026-11-23T12:00:00Z"}],
            {4: jobs(4, "2026-11-23T16:30:00Z")},
            "2026-11-23T17:10:00Z",
        )
        self.assertFalse(decision.should_read)
        self.assertIn("2026-11-24", decision.reason)

    def test_run_created_before_the_lookback_is_not_fetched_as_today(self):
        created = datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc)  # 10:00 Shanghai Nov 23
        day_start, day_end = daily_gate.shanghai_day_bounds(
            datetime(2026, 11, 24, 1, 30, tzinfo=timezone.utc)
        )
        self.assertFalse(daily_gate.in_search_window(created, day_start, day_end))

    def test_ignores_the_current_run(self):
        decision = self.decide(
            [{"id": 9, "created_at": "2026-11-23T17:20:00Z"}],
            {9: jobs(9, "2026-11-23T17:40:00Z")},
            "2026-11-23T21:30:00Z",
            current_run_id=9,
        )
        self.assertTrue(decision.should_read)

    def test_counts_a_read_that_started_before_midnight_and_finished_after(self):
        decision = self.decide(
            [{"id": 3, "created_at": "2026-11-23T15:30:00Z"}],
            {3: jobs(3, "2026-11-23T16:05:00Z")},
            "2026-11-23T18:00:00Z",
        )
        self.assertFalse(decision.should_read)

    def test_does_not_count_a_read_that_finished_before_midnight(self):
        decision = self.decide(
            [{"id": 3, "created_at": "2026-11-23T14:00:00Z"}],
            {3: jobs(3, "2026-11-23T15:30:00Z")},
            "2026-11-23T18:00:00Z",
        )
        self.assertTrue(decision.should_read)

    def test_shanghai_midnight_is_16_00_utc(self):
        start, end = daily_gate.shanghai_day_bounds(
            datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(start.isoformat(), "2026-11-24T00:00:00+08:00")
        self.assertEqual((end - start).days, 1)

    def test_cli_fixture_writes_github_output(self):
        fixture = {
            "now": "2026-11-23T21:30:00Z",
            "current_run_id": 99,
            "runs": [{"id": 1, "created_at": "2026-11-23T17:20:00Z"}],
            "jobs_by_run_id": {"1": jobs(1, "2026-11-23T17:40:00Z")},
        }
        output = ROOT / "tests" / ".github-output"
        env = os.environ.copy()
        env["DAILY_GATE_FIXTURE"] = json.dumps(fixture)
        env["GITHUB_OUTPUT"] = str(output)
        env.pop("GITHUB_RUN_ID", None)
        if output.exists():
            output.unlink()
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "daily_gate.py")],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("read=false", output.read_text(encoding="utf-8"))
        output.unlink()

    def test_live_check_failure_fails_open_without_calling_gh(self):
        output = ROOT / "tests" / ".github-output-fail-open"
        env = os.environ.copy()
        env.pop("DAILY_GATE_FIXTURE", None)
        env.pop("GITHUB_REPOSITORY", None)
        env.pop("GITHUB_RUN_ID", None)
        env["GITHUB_OUTPUT"] = str(output)
        env["PATH"] = "/usr/bin:/bin"
        if output.exists():
            output.unlink()
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "daily_gate.py")],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("proceeding with read", completed.stderr)
        self.assertIn("read=true", output.read_text(encoding="utf-8"))
        output.unlink()

    def test_workflow_has_spread_shanghai_schedules_and_keeps_the_step_name(self):
        text = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        self.assertIn(f"name: {daily_gate.DEPLOY_STEP}", text)
        schedules = []
        pending_cron = None
        cron_pattern = re.compile(r"- cron:\s*'([^']+)'")
        timezone_pattern = re.compile(r"timezone:\s*['\"]?([^'\"\s]+)")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                pending_cron = None
                continue
            cron_match = cron_pattern.search(line)
            if cron_match:
                pending_cron = cron_match.group(1)
                continue
            timezone_match = timezone_pattern.search(line) if pending_cron else None
            if timezone_match:
                schedules.append((pending_cron, timezone_match.group(1)))
                pending_cron = None
        self.assertEqual(
            schedules,
            [
                ("7 1 * * *", "Asia/Shanghai"),
                ("17 5 * * *", "Asia/Shanghai"),
                ("29 9 * * *", "Asia/Shanghai"),
                ("41 14 * * *", "Asia/Shanghai"),
                ("53 18 * * *", "Asia/Shanghai"),
            ],
        )
        for cron, _timezone_name in schedules:
            minute = cron.split()[0]
            self.assertNotEqual(minute, "0")
            self.assertNotEqual(minute, "*")
        self.assertNotIn("github.event_name == 'schedule'", text)
        self.assertIn("if: steps.daily.outputs.read == 'true'", text)
        self.assertIn("actions: read", text)
        self.assertIn("actions: write", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn('timezone: Asia/Shanghai', text)


if __name__ == "__main__":
    unittest.main()
