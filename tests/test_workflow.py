import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jobflow import workflow
from jobflow.reports import write_reports
from jobflow.storage import Store


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite3")
        self.config = {
            "search": {"min_score": 40, "target_tc": 500000, "max_packets_per_run": 5},
            "schedule": {"timezone": "America/Los_Angeles", "hour": 8, "minute": 0, "days": list(range(7))},
            "preparation": {"daily_minutes": 90, "horizon_days": 14, "timezone": "America/Los_Angeles"},
            "sources": [{"id": "example-board", "company": "Example", "kind": "greenhouse",
                         "board": "example", "url": "https://example.com/careers", "enabled": True}],
        }
        self.profile = {"name": "Candidate", "skills": ["Python", "Distributed systems"],
                        "evidence": [{"id": "experience-1", "text": "Built 3 distributed systems with Python.",
                                      "tags": ["Python", "Distributed systems"], "source_url": "https://example.org/resume"}]}
        self.raw = {"external_id": "123", "source_id": "example-board", "company": "Example",
                    "title": "Senior Software Engineer", "url": "https://example.com/jobs/123",
                    "location": "San Francisco, CA", "description": "Build distributed systems using Python.",
                    "department": "Infrastructure", "compensation": {}}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def ingest(self, raw=None, seen_at=None):
        return workflow.ingest_job(self.store, raw or self.raw, self.profile, self.config, seen_at=seen_at)

    def search_snapshot(self, jobs=None, error=None):
        with patch.object(workflow, "fetch_source", return_value=jobs or [], side_effect=error):
            return workflow.search(self.store, self.config, self.profile)

    def test_repeated_snapshot_updates_one_job_and_preserves_first_seen(self):
        first, fresh = self.ingest(seen_at="2026-10-28T15:00:00+00:00")
        self.assertTrue(fresh)
        self.search_snapshot([copy.deepcopy(self.raw)])
        result = self.search_snapshot([copy.deepcopy(self.raw)])
        jobs = self.store.all("jobs")
        self.assertEqual(1, len(jobs))
        self.assertEqual(first["first_seen"], jobs[0]["first_seen"])
        self.assertEqual(0, result["new_jobs"])
        self.assertEqual(1, result["matching_jobs"])

    def test_tracking_urls_and_aggregator_record_deduplicate(self):
        first, _ = self.ingest()
        imported = dict(self.raw, external_id="linkedin-789", source_id="linkedin",
                        url="https://example.com/jobs/123/?utm_source=linkedin&gh_src=abc")
        second, fresh = self.ingest(imported)
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(fresh)
        self.assertEqual(1, len(self.store.all("jobs")))

    def test_external_ids_from_different_boards_do_not_overwrite_distinct_jobs(self):
        first, _ = self.ingest()
        different = dict(self.raw, source_id="other-board", url="https://other.example.com/jobs/123",
                         title="Staff Software Engineer")
        second, fresh = self.ingest(different)
        self.assertTrue(fresh)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(2, len(self.store.all("jobs")))

    def test_source_failure_does_not_change_availability_or_missing_count(self):
        job, _ = self.ingest()
        result = self.search_snapshot(error=RuntimeError("Feed unavailable"))
        current = self.store.get("jobs", job["id"])
        self.assertEqual("open", current["availability"])
        self.assertEqual(0, current["missing_scans"])
        self.assertIn("example-board", result["source_errors"])
        self.assertEqual("error", self.store.get("source_health", "example-board")["status"])

    def test_closes_only_after_two_successful_missing_snapshots(self):
        job, _ = self.ingest()
        self.search_snapshot([])
        current = self.store.get("jobs", job["id"])
        self.assertEqual("missing", current["availability"])
        self.assertEqual(1, current["missing_scans"])
        self.assertEqual(1, len(workflow.ranked_jobs(self.store, self.config)))
        self.search_snapshot(error=RuntimeError("Timeout"))
        current = self.store.get("jobs", job["id"])
        self.assertEqual("missing", current["availability"])
        self.assertEqual(1, current["missing_scans"])
        self.search_snapshot([])
        self.assertEqual("closed", self.store.get("jobs", job["id"])["availability"])
        self.assertEqual([], workflow.ranked_jobs(self.store, self.config))

    def test_aggregator_import_does_not_disable_employer_closure_tracking(self):
        job, _ = self.ingest()
        self.ingest(dict(self.raw, external_id="linkedin-789", source_id="linkedin"))
        self.search_snapshot([])
        self.search_snapshot([])
        self.assertEqual("closed", self.store.get("jobs", job["id"])["availability"])

    def test_reappearing_job_reopens_and_resets_missing_counter(self):
        job, _ = self.ingest()
        self.search_snapshot([])
        self.search_snapshot([])
        result = self.search_snapshot([self.raw])
        current = self.store.get("jobs", job["id"])
        self.assertEqual("open", current["availability"])
        self.assertEqual(0, current["missing_scans"])
        self.assertEqual(0, result["new_jobs"])

    def test_disabled_and_manual_sources_do_not_close_jobs(self):
        job, _ = self.ingest()
        self.config["sources"][0]["enabled"] = False
        self.config["sources"].append({"id": "linkedin", "company": "Multiple", "kind": "manual", "url": "https://linkedin.com/jobs"})
        with patch.object(workflow, "fetch_source") as fetch:
            workflow.search(self.store, self.config, self.profile)
        fetch.assert_not_called()
        self.assertEqual("open", self.store.get("jobs", job["id"])["availability"])
        self.assertEqual("manual", self.store.get("source_health", "linkedin")["status"])

    def test_search_and_personalization_preserve_application_and_task_progress(self):
        job, _ = self.ingest()
        application = {"id": job["id"], "job_id": job["id"], "stage": "onsite", "notes": "Follow up next week"}
        task = {"id": "practice-1", "status": "done", "notes": "Reviewed tradeoffs", "title": "Cache design"}
        self.store.put("applications", application["id"], application)
        self.store.put("tasks", task["id"], task)
        self.search_snapshot([self.raw])
        workflow.personalize(self.store, self.config, self.profile)
        self.assertEqual(application, self.store.get("applications", application["id"]))
        self.assertEqual(task, self.store.get("tasks", task["id"]))

    def test_personalization_is_cached_until_relevant_inputs_change(self):
        job, _ = self.ingest()
        self.assertEqual(1, len(workflow.personalize(self.store, self.config, self.profile)))
        self.assertEqual([], workflow.personalize(self.store, self.config, self.profile))
        changed = self.store.get("jobs", job["id"])
        changed["department"] = "Storage"
        self.store.put("jobs", job["id"], changed)
        self.assertEqual(1, len(workflow.personalize(self.store, self.config, self.profile)))

    def test_zero_packet_limit_does_not_create_packets(self):
        self.ingest()
        self.assertEqual([], workflow.personalize(self.store, self.config, self.profile, limit=0))
        self.assertEqual([], self.store.all("packets"))

    def test_enabling_research_refreshes_previously_offline_packet(self):
        self.ingest()
        workflow.personalize(self.store, self.config, self.profile)
        calls = []

        def research(**kwargs):
            calls.append(kwargs)
            return {"text": "No confirmed leads found.", "sources": [], "mode": "research"}

        updated = workflow.personalize(self.store, self.config, self.profile, research=research)
        self.assertEqual(1, len(updated))
        self.assertEqual(1, len(calls))
        self.assertEqual("unverified", updated[0]["research_notes"]["status"])

    def test_repeated_complete_workflow_preserves_completed_preparation_task(self):
        with patch.object(workflow, "fetch_source", return_value=[self.raw]):
            first = workflow.run(self.store, self.config, self.profile)
        tasks = self.store.all("tasks")
        self.assertGreater(len(tasks), 0)
        completed = tasks[0]
        completed.update(status="done", notes="My own retrospective notes", title="My edited task title")
        self.store.put("tasks", completed["id"], completed)
        with patch.object(workflow, "fetch_source", return_value=[self.raw]):
            second = workflow.run(self.store, self.config, self.profile)
        self.assertEqual(1, first["new_jobs"])
        self.assertEqual(0, second["new_jobs"])
        self.assertEqual(0, second["packets_created"])
        self.assertEqual(len(tasks), len(self.store.all("tasks")))
        saved = self.store.get("tasks", completed["id"])
        self.assertEqual("done", saved["status"])
        self.assertEqual("My own retrospective notes", saved["notes"])
        self.assertEqual("My edited task title", saved["title"])

    def test_unavailable_research_is_retried_on_a_later_run(self):
        self.ingest()

        def unavailable(**kwargs):
            raise RuntimeError("Temporary research outage or call-budget limit")

        first = workflow.personalize(self.store, self.config, self.profile, research=unavailable)
        self.assertEqual("unavailable", first[0]["research_notes"]["status"])
        calls = []

        def recovered(**kwargs):
            calls.append(kwargs)
            return {"text": "No confirmed public lead.", "sources": [], "mode": "research"}

        retried = workflow.personalize(self.store, self.config, self.profile, research=recovered)
        self.assertEqual(1, len(retried))
        self.assertEqual(1, len(calls))
        self.assertEqual("unverified", retried[0]["research_notes"]["status"])

    def test_prepare_passes_progress_to_planner_and_does_not_delete_existing_tasks(self):
        old_task = {"id": "task-old", "status": "done", "notes": "Keep my notes"}
        application = {"id": "app-1", "stage": "technical"}
        interview = {"id": "interview-1", "application_id": "app-1"}
        new_task = {"id": "task-new", "status": "todo"}
        self.store.put("tasks", old_task["id"], old_task)
        self.store.put("applications", application["id"], application)
        self.store.put("interviews", interview["id"], interview)
        with patch.object(workflow, "build_plan", return_value={"tasks": [old_task, new_task], "markdown": "Plan"}) as planner:
            workflow.prepare(self.store, self.config, self.profile)
        args = planner.call_args.args
        self.assertEqual([application], args[1])
        self.assertEqual([interview], args[2])
        self.assertEqual([old_task], args[3])
        self.assertEqual(old_task, self.store.get("tasks", old_task["id"]))
        self.assertEqual(2, len(self.store.all("tasks")))
        self.assertEqual(application, self.store.get("applications", application["id"]))

    def test_scheduler_uses_pacific_clock_and_local_date_for_daily_dedup(self):
        before = datetime(2026, 10, 28, 14, 59, tzinfo=timezone.utc)
        due = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
        self.assertFalse(workflow.scheduled_due(self.store, self.config, before))
        self.assertTrue(workflow.scheduled_due(self.store, self.config, due))
        self.store.put("schedule", "daily", {"completed_date": "2026-10-28"})
        self.assertFalse(workflow.scheduled_due(self.store, self.config, due))
        self.assertFalse(workflow.scheduled_due(self.store, self.config, datetime(2026, 10, 29, 0, 30, tzinfo=timezone.utc)))
        self.assertTrue(workflow.scheduled_due(self.store, self.config, datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)))

    def test_scheduler_honors_daylight_saving_shift_and_weekdays(self):
        self.assertFalse(workflow.scheduled_due(self.store, self.config, datetime(2026, 11, 1, 15, 59, tzinfo=timezone.utc)))
        self.assertTrue(workflow.scheduled_due(self.store, self.config, datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)))
        self.config["schedule"]["days"] = [0, 1, 2, 3, 4]
        self.assertFalse(workflow.scheduled_due(self.store, self.config, datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc)))

    def test_successful_run_marks_local_date_and_failed_run_does_not(self):
        fixed = datetime(2026, 10, 29, 0, 30, tzinfo=timezone.utc)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz)

        with patch.object(workflow, "datetime", FixedDateTime), patch.object(workflow, "fetch_source", return_value=[self.raw]), \
                patch.object(workflow, "prepare", return_value={"tasks": []}):
            workflow.run(self.store, self.config, self.profile)
        self.assertEqual("2026-10-28", self.store.get("schedule", "daily")["completed_date"])
        self.assertEqual("completed", self.store.recent_runs()[0]["status"])
        previous_schedule = self.store.get("schedule", "daily")
        with patch.object(workflow, "search", side_effect=RuntimeError("Unexpected failure")):
            with self.assertRaises(RuntimeError):
                workflow.run(self.store, self.config, self.profile)
        self.assertEqual(previous_schedule, self.store.get("schedule", "daily"))
        self.assertEqual("failed", self.store.recent_runs()[0]["status"])

    def test_early_manual_run_does_not_consume_morning_schedule(self):
        fixed = datetime(2026, 10, 28, 7, 30, tzinfo=timezone.utc)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz)

        with patch.object(workflow, "datetime", FixedDateTime), patch.object(workflow, "fetch_source", return_value=[self.raw]), \
                patch.object(workflow, "prepare", return_value={"tasks": []}):
            workflow.run(self.store, self.config, self.profile)
        self.assertIsNone(self.store.get("schedule", "daily"))
        self.assertTrue(workflow.scheduled_due(self.store, self.config, datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)))

    def test_report_generation_exports_state_without_changing_progress(self):
        self.ingest()
        workflow.personalize(self.store, self.config, self.profile)
        task = {"id": "task-1", "status": "done", "notes": "Preserve this"}
        self.store.put("tasks", task["id"], task)
        self.store.put("plans", "current", {"id": "current", "markdown": "# Preparation\n"})
        report = write_reports(self.root, self.store, self.config)
        self.assertTrue(report.is_file())
        self.assertEqual([task], json.loads((self.root / "reports/tasks.json").read_text()))
        self.assertEqual(task, self.store.get("tasks", task["id"]))
        self.assertEqual(1, len(list((self.root / "reports/packets").glob("*.md"))))
        self.assertEqual(1, len(list((self.root / "reports/resumes").glob("*.md"))))

    def test_storage_bulk_write_rolls_back_if_any_record_is_invalid(self):
        original = {"id": "task-1", "status": "done"}
        self.store.put("tasks", original["id"], original)
        with self.assertRaises(KeyError):
            self.store.put_many("tasks", [{"id": "task-1", "status": "todo"}, {"status": "todo"}])
        self.assertEqual(original, self.store.get("tasks", original["id"]))


if __name__ == "__main__":
    unittest.main()
