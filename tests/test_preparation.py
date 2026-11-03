import copy
import unittest
from collections import defaultdict
from datetime import date, datetime, timezone

from jobflow.preparation import build_plan


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.config = {"preparation": {"daily_minutes": 90, "horizon_days": 14},
                       "schedule": {"timezone": "America/Los_Angeles"}}
        self.profile = {"skills": ["C++", "Go", "gRPC", "OpenTelemetry", "RDMA", "GKE", "storage"]}
        self.apps = [{"job_id": "openai-job", "company": "OpenAI", "title": "Senior Software Engineer", "stage": "technical"}]
        self.interviews = [{"id": "interview-1", "job_id": "openai-job", "company": "OpenAI",
                            "round": "technical", "starts_at": "2026-11-06T10:00:00-08:00"}]

    def plan(self, **kwargs):
        values = {"profile": self.profile, "applications": self.apps, "interviews": self.interviews,
                  "existing_tasks": [], "config": self.config, "today": date(2026, 10, 28)}
        values.update(kwargs)
        return build_plan(**values)

    def assert_budget(self, tasks, cap=90):
        totals = defaultdict(int)
        for task in tasks:
            if task["status"] not in ("skipped", "unscheduled"):
                totals[task["due_date"]] += task["minutes"]
        self.assertTrue(totals)
        self.assertTrue(all(amount <= cap for amount in totals.values()), totals)

    def test_default_plan_has_generic_bank_sources_and_budget(self):
        plan = self.plan()
        self.assert_budget(plan["tasks"])
        for task in plan["tasks"]:
            self.assertLessEqual(task["due_date"], "2026-11-06")
            if task["practice_key"] != "process":
                self.assertIn("Generic practice", task["notes"])
        self.assertIn("https://openai.com/interview-guide/", plan["markdown"])
        self.assertIn("Past questions", plan["markdown"])

    def test_identical_rerun_preserves_exact_tasks_and_no_duplicates(self):
        first = self.plan()["tasks"]
        second = self.plan(existing_tasks=first)["tasks"]
        self.assertEqual(first, second)
        self.assertEqual(len(second), len({t["id"] for t in second}))

    def test_preserves_user_text_effort_completion_and_custom_fields(self):
        original = self.plan()["tasks"]
        selected = original[0]
        selected.update(title="My custom exercise", notes="Focus on my exact failure case", status="done", minutes=20,
                        confidence=4, source_url="https://example.org/my-notes")
        snapshot = copy.deepcopy(original)
        result = self.plan(existing_tasks=original)
        self.assertEqual(next(t for t in result["tasks"] if t["id"] == selected["id"]), selected)
        self.assertEqual(original, snapshot, "Caller-owned records must not be mutated")
        self.assert_budget(result["tasks"])

    def test_new_earlier_interview_prioritized_and_budget_shared(self):
        interviews = self.interviews + [{"id": "near", "job_id": "near-job", "company": "Snowflake",
                                        "round": "system design", "starts_at": "2026-10-29T10:00:00-07:00"}]
        plan = self.plan(interviews=interviews)
        today_tasks = [t for t in plan["tasks"] if t["due_date"] == "2026-10-28"]
        self.assertTrue(today_tasks)
        self.assertEqual({t["company"] for t in today_tasks}, {"Snowflake"})
        self.assertTrue(any(t["company"] == "OpenAI" for t in plan["tasks"]))
        self.assert_budget(plan["tasks"])

    def test_new_interview_overrides_existing_general_plan(self):
        general = self.plan(applications=[], interviews=[])["tasks"]
        near = [{**self.interviews[0], "starts_at": "2026-10-31T10:00:00-07:00"}]
        result = self.plan(interviews=near, existing_tasks=general)
        scheduled = [t for t in result["tasks"] if t["status"] == "todo"]
        self.assertTrue(scheduled)
        self.assertTrue(all(t["company"] == "OpenAI" for t in scheduled))
        self.assertTrue(all(t["due_date"] <= "2026-10-31" for t in scheduled))
        self.assert_budget(result["tasks"])

    def test_new_nearer_interview_preempts_existing_farther_curriculum(self):
        original = self.plan()["tasks"]
        near = {"id": "new-near", "job_id": "snowflake-job", "company": "Snowflake", "round": "coding",
                "starts_at": "2026-10-30T10:00:00-07:00"}
        result = self.plan(interviews=[near] + self.interviews, existing_tasks=original)
        todays = [t for t in result["tasks"] if t["due_date"] == "2026-10-28" and t["status"] == "todo"]
        self.assertTrue(todays)
        self.assertEqual({t["company"] for t in todays}, {"Snowflake"})
        self.assert_budget(result["tasks"])

    def test_review_sessions_name_distinct_practice_topics(self):
        result = self.plan(applications=[], interviews=[])
        by_day = defaultdict(list)
        for task in result["tasks"]:
            if task.get("review_of_practice_key"):
                by_day[task["due_date"]].append(task)
                self.assertTrue(task["review_of_task_id"].startswith("prep-"))
        self.assertTrue(by_day)
        for day_tasks in by_day.values():
            self.assertEqual(len(day_tasks), len({t["title"] for t in day_tasks}))
        self.assertNotIn("Review your weakest interview topic", result["markdown"])

    def test_reschedule_earlier_keeps_ids_and_edits_without_overflow(self):
        original = self.plan()["tasks"]
        original[-1]["notes"] = "My manually written practice note"
        moved = copy.deepcopy(self.interviews)
        moved[0]["starts_at"] = "2026-10-29T10:00:00-07:00"
        plan = self.plan(interviews=moved, existing_tasks=original)
        self.assertTrue(any(t["status"] == "unscheduled" for t in plan["tasks"]))
        for task in plan["tasks"]:
            if task["status"] != "unscheduled":
                self.assertLessEqual(task["due_date"], "2026-10-29")
            else:
                self.assertEqual(task["due_date"], "")
        self.assertEqual({t["id"] for t in original}, {t["id"] for t in plan["tasks"]})
        self.assertEqual(next(t for t in plan["tasks"] if t["id"] == original[-1]["id"])["notes"], original[-1]["notes"])
        self.assert_budget(plan["tasks"])
        self.assertIn("Tasks awaiting rescheduling", plan["markdown"])

    def test_unscheduled_work_returns_when_interview_moves_later(self):
        original = self.plan()["tasks"]
        moved = copy.deepcopy(self.interviews)
        moved[0]["starts_at"] = "2026-10-28T18:00:00-07:00"
        compressed = self.plan(interviews=moved, existing_tasks=original)["tasks"]
        restored = self.plan(existing_tasks=compressed)["tasks"]
        self.assertFalse(any(t["status"] == "unscheduled" for t in restored))
        self.assertEqual({t["id"] for t in original}, {t["id"] for t in restored})
        self.assert_budget(restored)

    def test_deleted_interview_does_not_leave_scheduled_old_tasks(self):
        original = self.plan()["tasks"]
        result = self.plan(interviews=[], existing_tasks=original)["tasks"]
        old_ids = {t["id"] for t in original}
        old_tasks = [t for t in result if t["id"] in old_ids]
        self.assertTrue(all(t["status"] == "unscheduled" for t in old_tasks))
        self.assertTrue(any(t["target_key"].startswith("application:") for t in result if t["status"] == "todo"))

    def test_timezone_conversion_uses_local_interview_date(self):
        interviews = [{**self.interviews[0], "starts_at": "2026-10-30T01:00:00+00:00"}]
        result = self.plan(interviews=interviews)
        self.assertTrue(all(t["due_date"] <= "2026-10-29" for t in result["tasks"]))

    def test_timezone_conversion_after_dst_fallback(self):
        interviews = [{**self.interviews[0], "starts_at": "2026-11-02T07:30:00+00:00"}]
        result = self.plan(interviews=interviews, today=datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc))
        self.assertTrue(all(t["due_date"] == "2026-11-01" for t in result["tasks"]))

    def test_past_same_day_interview_not_scheduled(self):
        interviews = [{**self.interviews[0], "starts_at": "2026-10-28T09:00:00-07:00"}]
        result = self.plan(interviews=interviews, today="2026-10-28T12:00:00-07:00")
        self.assertFalse(any(t.get("interview_id") == "interview-1" for t in result["tasks"]))

    def test_no_applications_still_produces_daily_preparation(self):
        result = self.plan(applications=[], interviews=[])
        self.assertEqual(len({t["due_date"] for t in result["tasks"]}), 14)
        self.assert_budget(result["tasks"])

    def test_existing_manual_task_reserves_capacity_and_is_preserved(self):
        custom = {"id": "custom", "due_date": "2026-10-28", "title": "Study my notes", "minutes": 75,
                  "status": "doing", "category": "custom", "company": "Personal", "source_url": "", "notes": "Keep exactly"}
        result = self.plan(existing_tasks=[custom])
        self.assertEqual(next(t for t in result["tasks"] if t["id"] == "custom"), custom)
        self.assert_budget(result["tasks"])

    def test_completed_task_history_is_not_rewritten(self):
        completed = {"id": "historic", "due_date": "2026-10-27", "title": "Long completed session", "minutes": 150,
                     "status": "done", "category": "custom", "company": "OpenAI", "source_url": "", "notes": "Historical"}
        result = self.plan(existing_tasks=[completed])
        self.assertEqual(next(t for t in result["tasks"] if t["id"] == "historic"), completed)

    def test_impossible_manual_effort_is_reported_without_mutation(self):
        custom = {"id": "custom", "due_date": "2026-10-28", "title": "Large task", "minutes": 120, "status": "todo"}
        before = copy.deepcopy(custom)
        with self.assertRaisesRegex(ValueError, "split it or increase"):
            self.plan(existing_tasks=[custom])
        self.assertEqual(custom, before)

    def test_live_research_attributed_once_per_company_and_failure_nonfatal(self):
        calls = []
        def research(role, prompt):
            calls.append((role, prompt))
            return {"text": "Official guide confirms live coding. Individual reports remain anecdotal.",
                    "sources": [{"url": "https://example.org/report", "title": "Dated report"}], "mode": "test"}
        result = self.plan(interviews=self.interviews + [{**self.interviews[0], "id": "later"}], research=research)
        self.assertEqual(len(calls), 1)
        self.assertIn("Do not bypass", calls[0][1])
        self.assertIn("https://example.org/report", result["markdown"])
        def failure(role, prompt):
            raise RuntimeError("private error payload")
        fallback = self.plan(research=failure)
        self.assertTrue(fallback["tasks"])
        self.assertIn("Live research unavailable", fallback["markdown"])
        self.assertNotIn("private error payload", fallback["markdown"])

    def test_uncited_research_not_misrepresented_as_verified(self):
        result = self.plan(research=lambda *_: {"text": "Unsupported claim", "sources": [], "mode": "model"})
        self.assertIn("Unverified research (no citations returned)", result["markdown"])


if __name__ == "__main__":
    unittest.main()
