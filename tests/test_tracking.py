import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobflow.cli import dispatch, parser
from jobflow.storage import Store
from jobflow.tracking import export_tracking, read_records, validate_record


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite3")
        self.config = {"preparation": {"daily_minutes": 90, "horizon_days": 14},
                       "schedule": {"timezone": "America/Los_Angeles"},
                       "search": {"min_score": 35, "target_tc": 500000}, "sources": []}
        self.profile = {"skills": ["Python"], "evidence": [{"text": "Built systems with Python"}]}
        self.job = {"id": "job-1", "company": "Example", "title": "Senior Software Engineer"}
        self.store.put("jobs", self.job["id"], self.job)
        self.task = {"id": "task-1", "due_date": "2030-01-01", "title": "Design a cache", "category": "system_design",
                     "company": "Example", "minutes": 45, "status": "doing", "source_url": "https://example.com/guide",
                     "notes": "Notes, including a comma\nand a second line", "generated": True,
                     "target_key": "interview:abc", "custom_data": {"confidence": 3}, "job_id": "job-1"}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_csv_task_roundtrip_keeps_metadata_multiline_notes_and_types(self):
        self.store.put("tasks", self.task["id"], self.task)
        export_tracking(self.store, self.root / "exports")
        rows = read_records(self.root / "exports/tasks.csv")
        result = validate_record("tasks", rows[0], self.store)
        self.assertEqual(result, self.task)
        self.assertEqual(self.store.get("tasks", "task-1"), self.task)

    def test_csv_edits_win_over_preserved_metadata(self):
        self.store.put("tasks", self.task["id"], self.task)
        export_tracking(self.store, self.root / "exports")
        path = self.root / "exports/tasks.csv"
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows[0].update(title="My replacement title", status="done", minutes="30")
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        result = validate_record("tasks", read_records(path)[0], self.store)
        self.assertEqual((result["title"], result["status"], result["minutes"]), ("My replacement title", "done", 30))
        self.assertTrue(result["generated"])
        self.assertEqual(result["target_key"], "interview:abc")

    def test_nested_csv_json_column_roundtrip(self):
        path = self.root / "research.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["id", "text", "sources_json"])
            writer.writeheader()
            writer.writerow({"id": "research-1", "text": "Evidence", "sources_json": json.dumps([{"url": "https://example.org", "title": "Source"}])})
        row = validate_record("research", read_records(path)[0], self.store)
        self.assertEqual(row["sources"][0]["url"], "https://example.org")

    def test_validation_does_not_mutate_input(self):
        original = dict(self.task, minutes="45")
        validate_record("tasks", original, self.store)
        self.assertEqual(original["minutes"], "45")

    def test_task_validation_rejects_invalid_dates_minutes_and_status(self):
        for change in ({"due_date": "2026-02-30"}, {"minutes": 0}, {"minutes": -1}, {"status": "complete"}, {"title": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_record("tasks", dict(self.task, **change), self.store)

    def test_interview_requires_known_job_and_timezone(self):
        row = {"id": "interview-1", "job_id": "job-1", "starts_at": "2026-11-01T11:00:00-08:00", "round": "coding"}
        self.assertEqual(validate_record("interviews", row, self.store)["company"], "Example")
        for change in ({"job_id": "missing"}, {"starts_at": "2026-11-01T11:00:00"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_record("interviews", dict(row, **change), self.store)

    def test_contacts_require_real_evidence_url_and_mutual_path(self):
        row = {"id": "person-1", "name": "Recruiter", "company": "Example", "profile_url": "https://example.com/profile"}
        self.assertEqual(validate_record("contacts", row, self.store)["relationship"], "public")
        for change in ({"profile_url": "https:missing-host"}, {"relationship": "mutual"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_record("contacts", dict(row, **change), self.store)

    def test_job_import_coerces_csv_boolean_without_claiming_remote(self):
        row = {"company": "Example", "title": "Senior Software Engineer", "url": "https://example.com/jobs/123",
               "location": "Seattle, WA", "description": "Build with Python.", "remote": "false"}
        self.assertIs(validate_record("jobs", row, self.store)["remote"], False)
        self.assertIs(validate_record("jobs", dict(row, remote="true"), self.store)["remote"], True)
        with self.assertRaises(ValueError):
            validate_record("jobs", dict(row, remote="perhaps"), self.store)

    def test_job_import_rejects_malformed_compensation_before_ingestion(self):
        row = {"company": "Example", "title": "Senior Software Engineer", "url": "https://example.com/jobs/123",
               "location": "Seattle, WA", "description": "Build with Python.", "compensation": "bad schema"}
        with self.assertRaises(ValueError):
            validate_record("jobs", row, self.store)

    def test_rejected_task_edit_does_not_poison_saved_state(self):
        existing = dict(self.task, generated=False, target_key="")
        self.store.put("tasks", existing["id"], existing)
        args = parser().parse_args(["--root", str(self.root), "task", "task-1", "--minutes", "91"])
        with self.assertRaises(ValueError):
            dispatch(args, self.store, self.config, self.profile)
        self.assertEqual(self.store.get("tasks", "task-1"), existing)

    def test_rejected_task_batch_does_not_commit_partial_edits(self):
        existing = dict(self.task, generated=False, target_key="")
        self.store.put("tasks", existing["id"], existing)
        path = self.root / "tasks.json"
        path.write_text(json.dumps([dict(existing, title="Edited"), dict(existing, id="oversized", minutes=91)]))
        args = parser().parse_args(["--root", str(self.root), "import", str(path), "--kind", "tasks"])
        with self.assertRaises(ValueError):
            dispatch(args, self.store, self.config, self.profile)
        self.assertEqual(self.store.all("tasks"), [existing])

    def test_malformed_csv_extra_is_actionable_validation_error(self):
        path = self.root / "tasks.csv"
        path.write_text('id,title,_extra\ntask-1,Title,[]\n')
        with self.assertRaises(ValueError):
            read_records(path)

    def test_surplus_csv_columns_are_actionable_validation_error(self):
        path = self.root / "tasks.csv"
        path.write_text('id,title\ntask-1,Title,unexpected\n')
        with self.assertRaises(ValueError):
            read_records(path)


if __name__ == "__main__":
    unittest.main()
