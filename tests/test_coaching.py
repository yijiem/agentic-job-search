import copy
import unittest
from datetime import datetime, timezone

from jobflow.coaching import build_coaching


class CoachingTests(unittest.TestCase):
    def setUp(self):
        self.config = {"preparation": {"daily_minutes": 90, "timezone": "America/Los_Angeles"},
                       "schedule": {"timezone": "America/Los_Angeles"}, "search": {"min_score": 40}}

    def coach(self, **kwargs):
        values = {"applications": [], "tasks": [], "activities": [], "interviews": [],
                  "research": [], "jobs": [], "config": self.config,
                  "today": "2026-11-12T12:00:00-08:00"}
        values.update(kwargs)
        return build_coaching(**values)

    def test_application_credit_uses_submission_date_and_survives_stage_changes(self):
        apps = [
            {"job_id": "one", "stage": "rejected", "applied_at": "2026-11-13T07:00:00+00:00"},
            {"job_id": "one", "stage": "applied", "applied_at": "2026-11-12T15:00:00-08:00"},
            {"job_id": "yesterday", "stage": "applied", "applied_at": "2026-11-12T07:59:00+00:00"},
            {"job_id": "tomorrow", "stage": "applied", "applied_at": "2026-11-13T08:00:00+00:00"},
        ]
        result = self.coach(applications=apps)
        self.assertEqual(result["progress"]["applications"]["logged"], 1)
        self.assertEqual(result["progress"]["applications"]["job_ids"], ["one"])

    def test_legacy_records_never_gain_invented_submission_or_completion_dates(self):
        result = self.coach(applications=[{"job_id": "old", "stage": "applied", "updated_at": "2026-11-12T12:00:00-08:00"}],
                            tasks=[{"id": "old-done", "status": "done", "due_date": "2026-11-12", "minutes": 45,
                                    "category": "coding", "actual_minutes": 40}])
        self.assertEqual(result["progress"]["applications"]["logged"], 0)
        self.assertEqual(result["progress"]["applications"]["undated"], 1)
        self.assertEqual(result["progress"]["coding"]["logged"], 0)
        self.assertIn("no valid completed_at", result["markdown"])

    def test_completion_date_and_actual_time_differ_from_task_plan(self):
        tasks = [
            {"id": "late", "status": "done", "due_date": "2026-11-10", "minutes": 45,
             "category": "coding", "completed_at": "2026-11-13T07:30:00+00:00", "actual_minutes": 35},
            {"id": "estimated", "status": "done", "due_date": "2026-11-12", "minutes": 30,
             "category": "agent_harness", "completed_at": "2026-11-12T14:00:00-08:00"},
        ]
        result = self.coach(tasks=tasks)
        self.assertEqual(result["progress"]["coding"]["logged"], 35)
        self.assertEqual(result["progress"]["learning"]["logged"], 0)
        self.assertEqual(result["progress"]["learning"]["estimated_completed_minutes"], 30)
        self.assertEqual(result["logged_preparation_minutes"], 35)
        self.assertEqual(result["estimated_completed_minutes"], 30)

    def test_task_actual_total_suppresses_linked_activity_duplicates_only_on_same_date(self):
        task = {"id": "session", "status": "done", "category": "coding", "minutes": 45,
                "completed_at": "2026-11-12T14:00:00-08:00", "actual_minutes": 40}
        activities = [
            {"id": "linked", "date": "2026-11-12", "category": "coding", "minutes": 35, "task_id": "session"},
            {"id": "other", "date": "2026-11-12", "category": "learning", "minutes": 20},
        ]
        result = self.coach(tasks=[task], activities=activities)
        self.assertEqual(result["logged_preparation_minutes"], 60)
        self.assertEqual(result["progress"]["coding"]["logged"], 40)
        self.assertEqual(result["excluded_activity_ids"], ["linked"])
        yesterday = self.coach(tasks=[task], activities=[{**activities[0], "date": "2026-11-11"}],
                               today="2026-11-11T12:00:00-08:00")
        self.assertEqual(yesterday["progress"]["coding"]["logged"], 35)

    def test_existing_manual_work_reserves_budget_without_being_rewritten(self):
        manual = {"id": "mine", "status": "doing", "due_date": "2026-11-12", "category": "custom",
                  "minutes": 75, "title": "My exact task", "notes": "Keep my notes"}
        before = copy.deepcopy(manual)
        result = self.coach(tasks=[manual])
        self.assertEqual(manual, before)
        self.assertEqual(sum(block["minutes"] for block in result["blocks"]), 90)
        self.assertEqual(result["blocks"][0]["task_id"], "mine")
        self.assertEqual(result["blocks"][1]["minutes"], 15)
        self.assertTrue(any("unplanned" in text for text in result["cautions"]))

    def test_targets_and_application_estimate_do_not_raise_existing_budget(self):
        config = copy.deepcopy(self.config)
        config["preparation"]["daily_minutes"] = 45
        result = self.coach(config=config)
        self.assertEqual(sum(block["minutes"] for block in result["blocks"]), 45)
        self.assertEqual(result["preparation_target_minutes"], 90)
        self.assertEqual(result["preparation_capacity_minutes"], 45)
        self.assertEqual(result["estimated_application_minutes"], 150)
        self.assertEqual(result["estimated_total_minutes"], 195)
        self.assertIn("budget is unchanged", result["markdown"])
        default = self.coach()
        self.assertEqual(default["estimated_total_minutes"], 240)

    def test_linked_activity_reduces_pending_task_reservation_and_goal_work(self):
        tasks = [{"id": category, "category": category, "status": "doing" if category == "coding" else "todo",
                  "due_date": "2026-11-12", "minutes": 30, "title": "My " + category, "notes": "Keep this"}
                 for category in ("coding", "learning", "system_design")]
        original = copy.deepcopy(tasks)
        for spent, expected_pending, expected_remaining in ((10, 20, 20), (30, 0, 0)):
            with self.subTest(spent=spent):
                activity = {"id": "partial", "task_id": "coding", "date": "2026-11-12",
                            "category": "coding", "minutes": spent}
                result = self.coach(tasks=tasks, activities=[activity])
                coding_block = next(block for block in result["blocks"] if block["task_id"] == "coding")
                self.assertEqual(coding_block["minutes"], expected_pending)
                self.assertEqual(result["progress"]["coding"]["logged"], spent)
                self.assertEqual(result["progress"]["coding"]["remaining"], expected_remaining)
                self.assertEqual(result["logged_preparation_minutes"] + sum(block["minutes"] for block in result["blocks"]), 90)
                self.assertFalse(any("exceed the" in text for text in result["cautions"]))
                self.assertEqual(result["unplanned_goal_minutes"], {"learning": 0, "coding": 0, "system_design": 0})
                self.assertEqual(tasks, original)

    def test_pending_task_logged_overrun_remains_visible_without_negative_reservation(self):
        tasks = [{"id": category, "category": category, "status": "todo", "due_date": "2026-11-12",
                  "minutes": 30, "title": category} for category in ("coding", "learning", "system_design")]
        result = self.coach(tasks=tasks, activities=[{"id": "overrun", "task_id": "coding", "date": "2026-11-12",
                            "category": "coding", "minutes": 40}])
        coding_block = next(block for block in result["blocks"] if block["task_id"] == "coding")
        self.assertEqual(coding_block["minutes"], 0)
        self.assertEqual(result["logged_preparation_minutes"], 40)
        self.assertIn("exceed the 90-minute preparation budget by 10", result["markdown"])

    def test_logged_overrun_has_no_additional_blocks_and_reports_actual_total(self):
        result = self.coach(activities=[{"id": "long", "date": "2026-11-12", "category": "coding", "minutes": 100}])
        self.assertEqual(result["logged_preparation_minutes"], 100)
        self.assertEqual(result["blocks"], [])
        self.assertIn("exceed the 90-minute preparation budget by 10", result["markdown"])

    def test_activity_and_completed_plan_estimate_do_not_consume_capacity_twice(self):
        task = {"id": "estimated", "status": "done", "category": "learning", "minutes": 30,
                "completed_at": "2026-11-12T14:00:00-08:00"}
        result = self.coach(tasks=[task], activities=[{"id": "actual-session", "task_id": "estimated",
                            "date": "2026-11-12", "category": "learning", "minutes": 20}])
        self.assertEqual(result["logged_preparation_minutes"], 20)
        self.assertEqual(sum(block["minutes"] for block in result["blocks"]), 60)

    def test_recorded_near_interview_prioritizes_available_time_and_ignores_cancelled(self):
        config = copy.deepcopy(self.config)
        config["preparation"]["daily_minutes"] = 30
        interviews = [
            {"id": "cancelled", "company": "Other", "starts_at": "2026-11-12T13:00:00-08:00", "round": "design", "status": "cancelled"},
            {"id": "next", "company": "Target", "starts_at": "2026-11-13T10:00:00-08:00", "round": "coding"},
            {"id": "past", "company": "Old", "starts_at": "2026-11-12T09:00:00-08:00", "round": "technical"},
        ]
        result = self.coach(config=config, interviews=interviews)
        self.assertEqual(result["blocks"][0]["category"], "coding")
        self.assertEqual([row["id"] for row in result["upcoming_interviews"]], ["next"])
        self.assertIn("not reported employer questions", result["markdown"])

    def test_only_relevant_sourced_learning_notes_are_recommended_with_target_first(self):
        notes = [
            {"id": "generic", "kind": "learning_resource", "text": "Agent exercise", "topics": ["agent_harness"], "priority": 1,
             "sources": [{"url": "https://course.example/agents"}], "observed_at": "2026-11-12"},
            {"id": "target", "kind": "learning_resource", "text": "Round practice", "job_id": "target-job", "topics": [], "priority": 7,
             "sources": [{"url": "https://employer.example/process"}], "observed_at": "2026-11-12"},
            {"id": "unrelated", "kind": "learning_resource", "text": "Unrelated", "topics": ["baking"], "sources": [{"url": "https://example.org/baking"}]},
            {"id": "unsourced", "kind": "learning_resource", "text": "Claim", "topics": ["coding"], "sources": []},
            {"id": "unsafe-link", "kind": "learning_resource", "text": "Claim", "topics": ["coding"], "sources": [{"url": "file:///private/notes"}]},
            {"id": "nonresource", "kind": "contact_research", "text": "Person", "topics": ["coding"], "sources": [{"url": "https://example.org/person"}]},
        ]
        jobs = [{"id": "target-job", "company": "Target", "title": "Software engineer", "score": 90, "eligible": True, "availability": "open"},
                {"id": "closed", "company": "Closed", "title": "Engineer", "score": 99, "eligible": True, "availability": "closed"}]
        result = self.coach(research=notes, jobs=jobs)
        self.assertEqual([row["id"] for row in result["resources"]], ["target", "generic"])
        self.assertEqual([row["id"] for row in result["relevant_jobs"]], ["target-job"])

    def test_checking_is_repeatable_and_never_mutates_records(self):
        values = {"applications": [{"job_id": "one", "stage": "saved"}],
                  "tasks": [{"id": "mine", "title": "Practice", "status": "todo", "due_date": "2026-11-12", "minutes": 30, "category": "coding"}],
                  "activities": [{"id": "log", "date": "2026-11-12", "category": "learning", "minutes": 10}]}
        before = copy.deepcopy(values)
        first = self.coach(**values)
        second = self.coach(**values)
        self.assertEqual(first, second)
        self.assertEqual(values, before)

    def test_uncategorized_task_actual_time_is_not_lost_or_assigned_by_guess(self):
        result = self.coach(tasks=[{"id": "custom", "status": "done", "category": "custom", "minutes": 30,
                            "actual_minutes": 30, "completed_at": "2026-11-12T14:00:00-08:00"}],
                            activities=[{"id": "linked", "task_id": "custom", "date": "2026-11-12", "category": "coding", "minutes": 30}])
        self.assertEqual(result["logged_preparation_minutes"], 30)
        self.assertEqual(result["progress"]["coding"]["logged"], 0)
        self.assertIn("Set the task category", result["markdown"])

    def test_configured_timezone_overrides_execution_host_and_handles_dst(self):
        result = self.coach(today=datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc),
                            applications=[{"job_id": "dst", "stage": "applied", "applied_at": "2026-11-02T07:00:00+00:00"}])
        self.assertEqual(result["date"], "2026-11-01")
        self.assertEqual(result["progress"]["applications"]["logged"], 1)


if __name__ == "__main__":
    unittest.main()
