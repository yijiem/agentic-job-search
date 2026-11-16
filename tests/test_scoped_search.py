"""Source retries must not look like fresh snapshots of untouched feeds."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from jobflow import cli, workflow
from jobflow.storage import Store


class ScopedSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite3")
        self.config = {
            "search": {"min_score": 40, "target_tc": 500000, "max_packets_per_run": 5},
            "sources": [
                {"id": "feed-a", "company": "Alpha", "kind": "greenhouse", "board": "alpha",
                 "url": "https://alpha.example/careers", "enabled": True},
                {"id": "feed-b", "company": "Beta", "kind": "greenhouse", "board": "beta",
                 "url": "https://beta.example/careers", "enabled": True},
                {"id": "manual", "company": "Multiple", "kind": "manual",
                 "url": "https://manual.example/jobs", "enabled": True},
                {"id": "disabled", "company": "Disabled", "kind": "greenhouse", "board": "disabled",
                 "url": "https://disabled.example/careers", "enabled": False},
            ],
        }
        self.profile = {
            "name": "Example Candidate", "skills": ["Python", "Distributed systems"],
            "evidence": [{"id": "example", "text": "Built distributed systems with Python.",
                          "tags": ["Python", "Distributed systems"],
                          "source_url": "https://example.org/resume"}],
        }

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def raw(self, source_id):
        return {"source_id": source_id, "external_id": "123", "company": source_id,
                "title": "Senior Software Engineer", "url": "https://" + source_id + ".example/jobs/123",
                "location": "San Francisco, CA", "description": "Build distributed systems with Python.",
                "department": "Infrastructure", "compensation": {}}

    def seed(self, source_id):
        job, _ = workflow.ingest_job(self.store, self.raw(source_id), self.profile, self.config,
                                     seen_at="2026-01-01T18:00:00+00:00")
        self.store.put("source_health", source_id, {
            "id": source_id, "status": "ok", "checked_at": "2026-01-01T18:00:00+00:00",
            "message": "Previous complete snapshot", "count": 1,
        })
        return job

    def record(self, kind, key):
        row = self.store.db.execute("SELECT * FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return tuple(row) if row else None

    def state(self):
        return [tuple(row) for row in self.store.db.execute("SELECT * FROM records ORDER BY kind,id")]

    def test_selected_feed_advances_only_its_own_lifecycle_and_health(self):
        alpha, beta = self.seed("feed-a"), self.seed("feed-b")
        beta.update(availability="missing", missing_scans=1, score=123)
        self.store.put("jobs", beta["id"], beta)
        untouched_job = self.record("jobs", beta["id"])
        untouched_health = self.record("source_health", "feed-b")

        for expected_status, expected_scans in (("missing", 1), ("closed", 2)):
            with patch.object(workflow, "fetch_source", return_value=[]) as fetch:
                result = workflow.search(self.store, self.config, self.profile, source_ids=["feed-a"])
            self.assertEqual(["feed-a"], [call.args[0]["id"] for call in fetch.call_args_list])
            current = self.store.get("jobs", alpha["id"])
            self.assertEqual(expected_status, current["availability"])
            self.assertEqual(expected_scans, current["missing_scans"])
            self.assertEqual(untouched_job, self.record("jobs", beta["id"]))
            self.assertEqual(untouched_health, self.record("source_health", "feed-b"))
            self.assertEqual([], result["manual_sources"])
            self.assertIsNone(self.store.get("source_health", "manual"))
        self.assertEqual("ok", self.store.get("source_health", "feed-a")["status"])

    def test_failed_selected_retry_does_not_close_jobs_or_touch_other_health(self):
        alpha, beta = self.seed("feed-a"), self.seed("feed-b")
        alpha.update(availability="missing", missing_scans=1)
        self.store.put("jobs", alpha["id"], alpha)
        untouched = (self.record("jobs", beta["id"]), self.record("source_health", "feed-b"))
        with patch.object(workflow, "fetch_source", side_effect=RuntimeError("Temporary feed outage")) as fetch:
            result = workflow.search(self.store, self.config, self.profile, source_ids=["feed-a"])
        self.assertEqual(1, fetch.call_count)
        self.assertEqual({"feed-a": "Temporary feed outage"}, result["source_errors"])
        current = self.store.get("jobs", alpha["id"])
        self.assertEqual(("missing", 1), (current["availability"], current["missing_scans"]))
        self.assertEqual("error", self.store.get("source_health", "feed-a")["status"])
        self.assertEqual(untouched, (self.record("jobs", beta["id"]), self.record("source_health", "feed-b")))

    def test_manual_only_scope_records_manual_requirement_without_network(self):
        alpha, manual = self.seed("feed-a"), self.seed("manual")
        before_alpha = self.record("jobs", alpha["id"])
        before_health = self.record("source_health", "feed-a")
        with patch.object(workflow, "fetch_source") as fetch:
            result = workflow.search(self.store, self.config, self.profile, source_ids=["manual"])
        fetch.assert_not_called()
        self.assertEqual(["manual"], result["manual_sources"])
        self.assertEqual(0, result["fetched_jobs"])
        self.assertEqual("manual", self.store.get("source_health", "manual")["status"])
        self.assertEqual("open", self.store.get("jobs", manual["id"])["availability"])
        self.assertEqual(before_alpha, self.record("jobs", alpha["id"]))
        self.assertEqual(before_health, self.record("source_health", "feed-a"))

    def test_full_configuration_protects_nonselected_authoritative_ownership(self):
        # An aggregate-tagged duplicate must not gain ownership merely because
        # its authoritative board was not among today's requested sources.
        original = self.seed("feed-b")
        aggregate = dict(self.raw("feed-b"), source_id="aggregate-import", external_id="aggregate-123",
                         title="Unverified aggregate title", description="Unverified aggregate description")
        config_before = copy.deepcopy(self.config)
        with patch.object(workflow, "fetch_source", return_value=[aggregate]):
            result = workflow.search(self.store, self.config, self.profile, source_ids=["feed-a"])
        current = self.store.get("jobs", original["id"])
        self.assertEqual("feed-b", current["source_id"])
        self.assertEqual(original["title"], current["title"])
        self.assertEqual(original["description"], current["description"])
        self.assertEqual(original["last_seen"], current["last_seen"])
        self.assertEqual(0, result["new_jobs"])
        self.assertEqual(1, len(self.store.all("jobs")))
        self.assertEqual(config_before, self.config)

    def test_unknown_disabled_and_empty_selections_fail_before_mutation_or_fetch(self):
        self.seed("feed-a")
        for selected in (["feed-a", "unknown"], ["manual", "disabled"], []):
            with self.subTest(selected=selected):
                before = self.state()
                config_before = copy.deepcopy(self.config)
                with patch.object(workflow, "fetch_source") as fetch:
                    with self.assertRaises(ValueError):
                        workflow.search(self.store, self.config, self.profile, source_ids=selected)
                fetch.assert_not_called()
                self.assertEqual(before, self.state())
                self.assertEqual(config_before, self.config)

    def test_default_scope_still_searches_all_enabled_sources(self):
        self.seed("feed-a")
        self.seed("feed-b")
        with patch.object(workflow, "fetch_source", side_effect=lambda source: [self.raw(source["id"])]) as fetch:
            result = workflow.search(self.store, self.config, self.profile)
        self.assertEqual({"feed-a", "feed-b"}, {call.args[0]["id"] for call in fetch.call_args_list})
        self.assertEqual(["manual"], result["manual_sources"])
        self.assertEqual(2, result["fetched_jobs"])
        self.assertEqual(0, result["new_jobs"])
        self.assertIsNone(self.store.get("source_health", "disabled"))

    def test_repeated_selection_does_not_count_one_snapshot_twice(self):
        alpha = self.seed("feed-a")
        with patch.object(workflow, "fetch_source", return_value=[]) as fetch:
            workflow.search(self.store, self.config, self.profile, source_ids=["feed-a", "feed-a"])
        self.assertEqual(1, fetch.call_count)
        current = self.store.get("jobs", alpha["id"])
        self.assertEqual(("missing", 1), (current["availability"], current["missing_scans"]))

    def test_cli_repeated_source_flags_reach_scoped_search(self):
        args = cli.parser().parse_args(["--root", str(self.root), "search",
                                       "--source", "feed-b", "--source", "feed-a"])
        with patch.object(cli, "make_researcher", return_value=None), \
                patch.object(cli, "search", return_value={}) as search, \
                patch.object(cli, "write_reports", return_value=self.root / "reports/review.md"), \
                redirect_stdout(io.StringIO()):
            cli.dispatch(args, self.store, self.config, self.profile)
        search.assert_called_once_with(self.store, self.config, self.profile, source_ids=["feed-b", "feed-a"])


if __name__ == "__main__":
    unittest.main()
