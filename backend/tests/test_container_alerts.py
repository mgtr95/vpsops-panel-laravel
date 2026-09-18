import unittest
from datetime import datetime, timedelta, timezone

from app.services.container_alerts_svc import (
    _detect_events,
    _snapshot,
    is_expected_recycler,
    scan_log_text,
    skip_log_scan,
)
from app.services.container_status import explain_container


CONFIG = {
    "events": {
        "crash": True,
        "restart_loop": True,
        "unhealthy": True,
        "oom": True,
        "restart_count": True,
    }
}

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


def queue_container(**kwargs):
    data = {
        "name": "demo-app_queue",
        "status": "running",
        "state": "running",
        "health": None,
        "restart_count": 0,
        "compose_service": "queue",
        "cmd": ["php", "artisan", "queue:work", "--tries=3", "--sleep=3", "--max-time=3600"],
        "exit_code": 0,
        "issue": None,
    }
    data.update(kwargs)
    return data


def app_container(**kwargs):
    data = {
        "name": "demo-app_app",
        "status": "running",
        "state": "running",
        "health": "healthy",
        "restart_count": 0,
        "compose_service": "app",
        "cmd": ["frankenphp", "run"],
        "exit_code": 0,
        "issue": None,
    }
    data.update(kwargs)
    return data


def kinds(events):
    return [event_type for event_type, _ in events]


def step(container, prev, extra, now):
    events, extra = _detect_events(container, prev, CONFIG, extra=extra, now=now)
    return events, _snapshot(container), extra


def attrs(*, status, exit_code=0, restarting=False, restart_count=0, oom=False, health=None):
    return {
        "RestartCount": restart_count,
        "State": {
            "Status": status,
            "ExitCode": exit_code,
            "Restarting": restarting,
            "OOMKilled": oom,
            "Error": "",
            "FinishedAt": "0001-01-01T00:00:00Z",
            "Health": health or {},
        },
    }


class RecyclerDetectionTests(unittest.TestCase):
    def test_panel_queue_service(self):
        self.assertTrue(is_expected_recycler(queue_container()))

    def test_name_suffix(self):
        self.assertTrue(is_expected_recycler({"name": "demo-app_queue", "cmd": []}))
        self.assertTrue(is_expected_recycler({"name": "app-horizon", "cmd": []}))
        self.assertFalse(is_expected_recycler({"name": "demo-app_app", "cmd": []}))

    def test_max_time_flag(self):
        self.assertTrue(
            is_expected_recycler(
                {"name": "custom", "cmd": ["php", "artisan", "queue:work", "--max-time=3600"]}
            )
        )


class QueueRecycleAlertTests(unittest.TestCase):
    def test_hourly_queue_recycle_is_silent(self):
        extra = {}
        running = queue_container(restart_count=4)
        events, prev, extra = step(running, None, extra, NOW)
        self.assertEqual(kinds(events), [])

        events, prev, extra = step(running, prev, extra, NOW + timedelta(minutes=30))
        self.assertEqual(kinds(events), [])

        restarting = queue_container(
            status="restarting",
            state="restarting",
            restart_count=5,
            exit_code=0,
        )
        events, prev, extra = step(restarting, prev, extra, NOW + timedelta(hours=1))
        self.assertEqual(kinds(events), [])

        after = queue_container(restart_count=5, exit_code=0)
        events, _, extra = step(after, prev, extra, NOW + timedelta(hours=1, seconds=5))
        self.assertEqual(kinds(events), [])

    def test_queue_recycle_count_bumps_after_it_is_running_again(self):
        extra = {}
        running = queue_container(restart_count=4)
        _, prev, extra = step(running, None, extra, NOW)

        restarting = queue_container(
            status="restarting",
            state="restarting",
            restart_count=4,
            exit_code=0,
        )
        events, prev, extra = step(restarting, prev, extra, NOW + timedelta(hours=1))
        self.assertEqual(kinds(events), [])

        after = queue_container(restart_count=5)
        events, _, extra = step(after, prev, extra, NOW + timedelta(hours=1, seconds=5))
        self.assertEqual(kinds(events), [])

    def test_missed_restarting_window_still_silent_for_queue(self):
        extra = {}
        running = queue_container(restart_count=2)
        events, prev, extra = step(running, None, extra, NOW)
        self.assertEqual(kinds(events), [])

        after = queue_container(restart_count=3)
        events, _, extra = step(after, prev, extra, NOW + timedelta(hours=1))
        self.assertEqual(kinds(events), [])

    def test_queue_stuck_restarting_still_alerts(self):
        extra = {}
        running = queue_container(restart_count=1)
        _, prev, extra = step(running, None, extra, NOW)

        restarting = queue_container(
            status="restarting",
            state="restarting",
            restart_count=2,
            exit_code=1,
            issue={"kind": "restarting", "title": "Keeps restarting"},
        )
        events, prev, extra = step(restarting, prev, extra, NOW + timedelta(seconds=30))
        self.assertEqual(kinds(events), [])

        events, _, extra = step(
            restarting, prev, extra, NOW + timedelta(seconds=60)
        )
        self.assertEqual(kinds(events), ["restart_loop"])

    def test_queue_crash_burst_alerts(self):
        extra = {}
        running = queue_container(restart_count=0)
        _, prev, extra = step(running, None, extra, NOW)

        for i, count in enumerate((1, 2, 3), start=1):
            nxt = queue_container(restart_count=count, exit_code=1)
            events, prev, extra = step(
                nxt, prev, extra, NOW + timedelta(seconds=30 * i)
            )
        self.assertIn("restart_loop", kinds(events))

    def test_queue_crash_exit_still_alerts_restarted(self):
        extra = {}
        running = queue_container(restart_count=1)
        _, prev, extra = step(running, None, extra, NOW)

        restarting = queue_container(
            status="restarting",
            state="restarting",
            restart_count=2,
            exit_code=1,
            issue={"kind": "restarting", "title": "Keeps restarting"},
        )
        events, prev, extra = step(restarting, prev, extra, NOW + timedelta(seconds=5))
        self.assertEqual(kinds(events), [])

        after = queue_container(restart_count=2, exit_code=0)
        events, _, extra = step(after, prev, extra, NOW + timedelta(seconds=10))
        self.assertEqual(kinds(events), ["restart_count"])


class OtherContainerAlertTests(unittest.TestCase):
    def test_single_brief_restarting_is_not_a_loop(self):
        extra = {}
        running = app_container()
        _, prev, extra = step(running, None, extra, NOW)

        restarting = app_container(
            status="restarting",
            state="restarting",
            restart_count=1,
            exit_code=1,
            health=None,
            issue={"kind": "restarting", "title": "Keeps restarting"},
        )
        events, prev, extra = step(restarting, prev, extra, NOW + timedelta(seconds=30))
        self.assertEqual(kinds(events), [])

        after = app_container(restart_count=1, exit_code=0)
        events, _, extra = step(after, prev, extra, NOW + timedelta(seconds=35))
        self.assertEqual(kinds(events), ["restart_count"])

    def test_docker_restart_sigterm_is_not_emailed(self):
        extra = {}
        running = app_container()
        _, prev, extra = step(running, None, extra, NOW)

        restarting = app_container(
            status="restarting",
            state="restarting",
            restart_count=1,
            exit_code=143,
            health=None,
        )
        events, prev, extra = step(restarting, prev, extra, NOW + timedelta(seconds=2))
        self.assertEqual(kinds(events), [])

        after = app_container(restart_count=1, exit_code=0)
        events, _, extra = step(after, prev, extra, NOW + timedelta(seconds=5))
        self.assertEqual(kinds(events), [])

    def test_docker_sigkill_recreate_is_not_emailed(self):
        extra = {}
        running = app_container()
        _, prev, extra = step(running, None, extra, NOW)

        restarting = app_container(
            status="restarting",
            state="restarting",
            restart_count=1,
            exit_code=137,
            health=None,
        )
        events, prev, extra = step(restarting, prev, extra, NOW + timedelta(seconds=2))
        self.assertEqual(kinds(events), [])

        after = app_container(restart_count=1, exit_code=0)
        events, _, extra = step(after, prev, extra, NOW + timedelta(seconds=5))
        self.assertEqual(kinds(events), [])

    def test_crash_and_unhealthy_still_alert(self):
        extra = {}
        running = app_container()
        _, prev, extra = step(running, None, extra, NOW)

        crashed = app_container(
            status="exited",
            state="exited",
            exit_code=1,
            health=None,
            issue={"kind": "crashed", "title": "Crashed"},
        )
        events, prev, extra = step(crashed, prev, extra, NOW + timedelta(seconds=30))
        self.assertEqual(kinds(events), ["crash"])

        events, _, extra = step(
            app_container(health="unhealthy", issue={"kind": "unhealthy", "title": "Not responding"}),
            prev,
            extra,
            NOW + timedelta(seconds=60),
        )
        self.assertEqual(kinds(events), ["unhealthy"])


class ExplainContainerTests(unittest.TestCase):
    def test_clean_exit_restarting_is_not_a_crash_loop(self):
        self.assertIsNone(explain_container(attrs(status="restarting", exit_code=0, restarting=True)))
        self.assertIsNone(explain_container(attrs(status="restarting", exit_code=143, restarting=True)))

    def test_crash_exit_restarting_is_still_an_issue(self):
        issue = explain_container(attrs(status="restarting", exit_code=1, restarting=True))
        self.assertEqual(issue["kind"], "restarting")

    def test_sigkill_without_oomkilled_is_not_oom(self):
        self.assertIsNone(
            explain_container(attrs(status="restarting", exit_code=137, restarting=True))
        )
        issue = explain_container(attrs(status="exited", exit_code=137))
        self.assertEqual(issue["kind"], "stopped")

    def test_oomkilled_is_still_oom(self):
        issue = explain_container(attrs(status="exited", exit_code=137, oom=True))
        self.assertEqual(issue["kind"], "oom")


class LogScanCheckpointTests(unittest.TestCase):
    OLD = (
        "2026-08-27T10:22:45.739395909Z "
        '{"level":"error","logger":"http.log.error","msg":"connection refused"}\n'
    )
    NEW_INFO = "2026-09-14T08:23:31.660837864Z {\"level\":\"info\",\"msg\":\"storage cleaning\"}\n"
    NEW_ERROR = (
        "2026-09-14T11:30:00.000000000Z "
        '{"level":"error","logger":"http.log.error","msg":"dial tcp: connection refused"}\n'
    )

    def test_new_info_line_does_not_resurrect_old_errors(self):
        matches, checkpoint = scan_log_text(self.OLD, None)
        self.assertEqual(matches, [])
        self.assertTrue(checkpoint.startswith("2026-08-27"))

        matches, checkpoint = scan_log_text(self.OLD + self.NEW_INFO, checkpoint)
        self.assertEqual(matches, [])
        self.assertTrue(checkpoint.startswith("2026-09-14"))

    def test_new_error_after_checkpoint_alerts(self):
        _, checkpoint = scan_log_text(self.OLD, None)
        matches, _ = scan_log_text(self.OLD + self.NEW_INFO + self.NEW_ERROR, checkpoint)
        self.assertEqual(len(matches), 1)
        self.assertIn("2026-09-14T11:30:00", matches[0])

    def test_sha256_checkpoint_is_silent_bootstrap(self):
        old_hash = "a" * 64
        blob = self.OLD + self.NEW_INFO
        matches, checkpoint = scan_log_text(blob, old_hash)
        self.assertEqual(matches, [])
        self.assertTrue(checkpoint.startswith("2026-09-14"))

    def test_skip_panel_dashboard_logs(self):
        self.assertTrue(skip_log_scan({"name": "vps_dashboard"}))
        self.assertFalse(skip_log_scan({"name": "nginx-nginx-1"}))


if __name__ == "__main__":
    unittest.main()
