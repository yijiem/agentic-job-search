import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from jobflow.sources import (SourceError, _request_json, canonical_url,
                             compensation_from_text, fetch_source, job_id, plain_text)


class SourceTests(unittest.TestCase):
    def source(self, kind="greenhouse"):
        return {"id": "acme", "company": "Acme", "kind": kind, "board": "acme"}

    def gh_job(self, **kwargs):
        job = {"id": 123, "title": "Senior Software Engineer", "location": {"name": "San Francisco"},
               "absolute_url": "https://example.com/jobs?gh_jid=123&utm_source=mail", "content": "<p>Build C++ systems.</p>",
               "departments": [{"name": "Engineering"}], "updated_at": "2026-10-01"}
        job.update(kwargs)
        return job

    def ashby_job(self, **kwargs):
        job = {"id": "abc", "title": "Software Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/abc",
               "location": "New York, NY", "secondaryLocations": [{"location": "San Francisco, CA"}],
               "descriptionPlain": "Work on distributed systems", "isRemote": True, "workplaceType": "Hybrid"}
        job.update(kwargs)
        return job

    def fetch(self, payload, kind="greenhouse"):
        with patch("jobflow.sources._request_json", return_value=payload):
            return fetch_source(self.source(kind))

    def test_greenhouse_normalizes_and_preserves_identity(self):
        job = self.fetch({"jobs": [self.gh_job()]})[0]
        self.assertEqual(job["external_id"], "123")
        self.assertEqual(job["url"], "https://example.com/jobs?gh_jid=123")
        self.assertEqual(job["description"], "Build C++ systems.")
        self.assertIsNone(job["posted_at"], "Modification time is not posting time")
        self.assertFalse(job["remote"])

    def test_duplicate_external_id_is_merged(self):
        self.assertEqual(len(self.fetch({"jobs": [self.gh_job(), self.gh_job()]})), 1)

    def test_canonical_url_keeps_job_query(self):
        self.assertEqual(canonical_url("https://EXAMPLE.com/job/?gh_jid=12&foo=3&utm_campaign=x&gh_src=a#apply"),
                         "https://example.com/job?foo=3&gh_jid=12")
        self.assertNotEqual(job_id({"company": "A", "url": "https://a.test/job?gh_jid=1"}),
                            job_id({"company": "A", "url": "https://a.test/job?gh_jid=2"}))

    def test_external_id_is_scoped_to_source(self):
        a = {"company": "ACME", "external_id": 123, "source_id": "one"}
        b = {"company": "Acme", "external_id": "123", "source_id": "one"}
        self.assertEqual(job_id(a), job_id(b))
        self.assertNotEqual(job_id(a), job_id(dict(b, source_id="two")))
        self.assertNotEqual(job_id(a), job_id(dict(b, company="Other")))

    def test_id_without_identity_rejected(self):
        with self.assertRaises(ValueError):
            job_id({"company": "A"})

    def test_html_entities_and_scripts_removed(self):
        self.assertEqual(plain_text("&lt;p&gt;C++ &amp; Python&lt;/p&gt;<script>hidden</script>"), "C++ & Python")

    def test_malformed_payload_is_error_not_empty_board(self):
        for payload in ({}, {"jobs": None}, {"jobs": {}}, [], {"jobs": [None]}, {"jobs": [{}]}):
            with self.subTest(payload=payload), self.assertRaises(SourceError):
                self.fetch(payload)

    def test_bad_job_url_rejected(self):
        with self.assertRaises(SourceError):
            self.fetch({"jobs": [self.gh_job(absolute_url="javascript:alert(1)")]})

    def test_manual_never_fetches(self):
        with patch("jobflow.sources._request_json") as request:
            self.assertEqual(fetch_source({"kind": "manual"}), [])
            request.assert_not_called()

    def test_invalid_board_token(self):
        for board in ("https://private.test", "../acme", "..", "acme/jobs", "acme?query=x"):
            with self.subTest(board=board), self.assertRaises(SourceError):
                fetch_source(dict(self.source(), board=board))

    def test_dotted_ashby_board_token(self):
        with patch("jobflow.sources._request_json", return_value={"jobs": [self.ashby_job()]}) as request:
            jobs = fetch_source(dict(self.source("ashby"), board="far.ai"))
        self.assertEqual(len(jobs), 1)
        request.assert_called_once_with(
            "https://api.ashbyhq.com/posting-api/job-board/far.ai?includeCompensation=true")

    def test_ashby_hybrid_is_not_remote_and_keeps_secondary_locations(self):
        job = self.fetch({"jobs": [self.ashby_job()]}, "ashby")[0]
        self.assertFalse(job["remote"])
        self.assertIn("San Francisco", job["location"])

    def test_ashby_unlisted_posting_skipped(self):
        self.assertEqual(self.fetch({"jobs": [self.ashby_job(isListed=False)]}, "ashby"), [])

    def test_ashby_salary_is_not_total_comp(self):
        compensation = {"compensationTierSummary": "$200K–$300K + equity", "summaryComponents": [
            {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 200000, "maxValue": 300000},
            {"compensationType": "EquityPercentage", "interval": "NONE", "minValue": 0.1, "maxValue": 0.5}]}
        comp = self.fetch({"jobs": [self.ashby_job(compensation=compensation)]}, "ashby")[0]["compensation"]
        self.assertEqual(comp["base_max"], 300000)
        self.assertIsNone(comp["tc_max"])
        self.assertEqual(comp["tc_status"], "base_only")

    def test_ashby_hourly_not_treated_as_annual(self):
        compensation = {"summaryComponents": [{"compensationType": "Salary", "interval": "1 HOUR", "currencyCode": "USD", "minValue": 100, "maxValue": 200}]}
        comp = self.fetch({"jobs": [self.ashby_job(compensation=compensation)]}, "ashby")[0]["compensation"]
        self.assertIsNone(comp["base_max"])

    def test_explicit_total_comp_and_base_are_separate(self):
        comp = compensation_from_text("Base salary: $200,000 - $300,000. Total compensation: $450k–$650k annually.")
        self.assertEqual((comp["base_min"], comp["base_max"]), (200000, 300000))
        self.assertEqual((comp["tc_min"], comp["tc_max"]), (450000, 650000))
        self.assertEqual(comp["tc_status"], "published_total_compensation")

    def test_generic_compensation_not_claimed_as_total(self):
        comp = compensation_from_text("Compensation: $200,000 - $400,000 plus equity and benefits.")
        self.assertIsNone(comp["tc_max"])
        self.assertEqual(comp["tc_status"], "unknown")

    def test_base_plus_unknown_equity_is_base_only(self):
        comp = compensation_from_text("Annual salary range is $200K - $300K. Equity and bonuses are additional.")
        self.assertEqual(comp["base_max"], 300000)
        self.assertIsNone(comp["tc_max"])

    def test_currency_and_mixed_currency_not_combined(self):
        comp = compensation_from_text("Base salary: CAD $200,000 - $300,000")
        self.assertEqual(comp["currency"], "CAD")
        comp = compensation_from_text("Base salary: USD $200,000 - $300,000. Base salary: CAD $150,000 - $250,000")
        self.assertIsNone(comp["base_max"])
        self.assertTrue(comp["raw"])

    def test_lever_salary_and_description(self):
        job = {"id": "1", "text": "Software Engineer", "hostedUrl": "https://jobs.lever.co/acme/1",
               "categories": {"allLocations": ["San Francisco", "Remote - United States"], "commitment": "Full-time"},
               "descriptionPlain": "Build", "lists": [{"text": "Requirements", "content": "<li>Python</li>"}],
               "salaryRange": {"min": 200000, "max": 300000, "currency": "USD", "interval": "per-year-salary"}}
        normalized = self.fetch([job], "lever")[0]
        self.assertIn("Python", normalized["description"])
        self.assertEqual(normalized["compensation"]["base_max"], 300000)
        self.assertIsNone(normalized["compensation"]["tc_max"])

    def test_bad_json_rejected_without_retry(self):
        with patch("jobflow.sources.urlopen", return_value=io.BytesIO(b"bad")) as request:
            with self.assertRaises(SourceError):
                _request_json("https://a.test")
            self.assertEqual(request.call_count, 1)

    def test_response_at_size_limit_is_accepted(self):
        payload = b'{"jobs": []}'
        with patch("jobflow.sources.MAX_RESPONSE_BYTES", len(payload)), \
                patch("jobflow.sources.urlopen", return_value=io.BytesIO(payload)):
            self.assertEqual(_request_json("https://a.test"), {"jobs": []})

    def test_oversized_response_is_bounded_and_not_retried(self):
        class RecordingResponse(io.BytesIO):
            def read(self, size=-1):
                self.requested_size = size
                return super().read(size)

        limit = 1024 * 1024
        response = RecordingResponse(b' ' * (limit + 100))
        with patch("jobflow.sources.MAX_RESPONSE_BYTES", limit), \
                patch("jobflow.sources.urlopen", return_value=response) as request, \
                patch("jobflow.sources.time.sleep") as sleep:
            with self.assertRaisesRegex(SourceError, "exceeds the 1 MiB limit"):
                _request_json("https://a.test")
        self.assertEqual(response.requested_size, limit + 1)
        request.assert_called_once()
        sleep.assert_not_called()

    def test_auth_block_not_retried(self):
        with patch("jobflow.sources.urlopen", side_effect=HTTPError("https://a.test", 403, "Forbidden", {}, None)) as request:
            with patch("jobflow.sources.time.sleep") as sleep, self.assertRaisesRegex(SourceError, "HTTP 403"):
                _request_json("https://a.test")
            self.assertEqual(request.call_count, 1)
            sleep.assert_not_called()

    def test_transient_errors_retry_with_capped_backoff(self):
        error = HTTPError("https://a.test", 429, "Rate limited", {"Retry-After": "999"}, None)
        with patch("jobflow.sources.urlopen", side_effect=[error, io.BytesIO(b'{"jobs": []}')]) as request:
            with patch("jobflow.sources.time.sleep") as sleep:
                self.assertEqual(_request_json("https://a.test"), {"jobs": []})
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(5.0)

    def test_network_retry_budget_is_bounded(self):
        with patch("jobflow.sources.urlopen", side_effect=URLError("unavailable")) as request:
            with patch("jobflow.sources.time.sleep"), self.assertRaises(SourceError):
                _request_json("https://a.test")
            self.assertEqual(request.call_count, 3)


if __name__ == "__main__":
    unittest.main()
