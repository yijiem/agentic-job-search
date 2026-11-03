import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from jobflow.provider import ResearchError, Researcher


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-not-a-real-key", "OPENAI_MODEL": "unit-test-model"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def response(self, **kwargs):
        result = {"status": "completed", "output": [
            {"type": "web_search_call", "action": {"sources": [{"url": "https://example.com/posting", "title": "Posting"}]}},
            {"type": "message", "content": [{"type": "output_text", "text": "Cited research.",
                                                "annotations": [{"type": "url_citation", "url": "https://example.com/posting", "title": "Posting"}]}]}],
            "usage": {"input_tokens": 100, "output_tokens": 20}}
        result.update(kwargs)
        return io.BytesIO(json.dumps(result).encode())

    def test_success_deduplicates_sources_and_retains_usage(self):
        researcher = Researcher({"max_calls_per_run": 2, "max_output_tokens": 1000})
        with patch("jobflow.provider.urllib.request.urlopen", return_value=self.response()) as request:
            result = researcher("test role", "Research public sources")
        self.assertEqual(result["text"], "Cited research.")
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["usage"]["output_tokens"], 20)
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual(body["max_output_tokens"], 1000)
        self.assertFalse(body["store"])
        self.assertEqual(body["tools"], [{"type": "web_search"}])

    def test_call_budget_stops_before_another_paid_request(self):
        researcher = Researcher({"max_calls_per_run": 1})
        with patch("jobflow.provider.urllib.request.urlopen", return_value=self.response()) as request:
            researcher("role", "first")
            with self.assertRaisesRegex(ResearchError, "budget"):
                researcher("role", "second")
        self.assertEqual(request.call_count, 1)

    def test_failed_attempt_consumes_budget_and_does_not_retry(self):
        researcher = Researcher({"max_calls_per_run": 1})
        failure = HTTPError("https://api.openai.com/v1/responses", 429, "Rate limited", {}, None)
        with patch("jobflow.provider.urllib.request.urlopen", side_effect=failure) as request:
            with self.assertRaisesRegex(ResearchError, "HTTP 429"):
                researcher("role", "first")
            with self.assertRaisesRegex(ResearchError, "budget"):
                researcher("role", "second")
        self.assertEqual(request.call_count, 1)

    def test_http_error_does_not_expose_credentials(self):
        failure = HTTPError("https://api.openai.com/v1/responses", 401, "unit-test-not-a-real-key", {}, None)
        with patch("jobflow.provider.urllib.request.urlopen", side_effect=failure):
            with self.assertRaises(ResearchError) as caught:
                Researcher({})("role", "prompt")
        self.assertNotIn("unit-test-not-a-real-key", str(caught.exception))

    def test_transport_and_invalid_json_become_research_errors(self):
        for response in (URLError("unavailable"), io.BytesIO(b"not JSON")):
            with self.subTest(response=type(response).__name__):
                kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with patch("jobflow.provider.urllib.request.urlopen", **kwargs), self.assertRaises(ResearchError):
                    Researcher({})("role", "prompt")

    def test_incomplete_and_empty_responses_rejected(self):
        for changes in ({"status": "incomplete"}, {"output": []}):
            with self.subTest(changes=changes):
                with patch("jobflow.provider.urllib.request.urlopen", return_value=self.response(**changes)), self.assertRaises(ResearchError):
                    Researcher({})("role", "prompt")

    def test_missing_credentials_fail_before_network(self):
        with patch.dict(os.environ, {}, clear=True), patch("jobflow.provider.urllib.request.urlopen") as request:
            with self.assertRaises(ResearchError):
                Researcher({})
            request.assert_not_called()

    def test_malformed_response_schema_becomes_research_error(self):
        for payload in ([], {"status": "completed", "output": None}, {"status": "completed", "output": [None]},
                        {"status": "completed", "output": [{"content": [{"type": "output_text", "text": None}]}]}):
            with self.subTest(payload=payload):
                response = io.BytesIO(json.dumps(payload).encode())
                with patch("jobflow.provider.urllib.request.urlopen", return_value=response), self.assertRaises(ResearchError):
                    Researcher({})("role", "prompt")

    def test_citations_must_have_real_http_hosts(self):
        payload = self.response(output=[{"type": "message", "content": [{"type": "output_text", "text": "No verified citation.",
            "annotations": [{"url": "https:missing-host"}, {"url": "javascript:alert(1)"}, {"url": "https://example.org/valid"}]}]}])
        with patch("jobflow.provider.urllib.request.urlopen", return_value=payload):
            result = Researcher({})("role", "prompt")
        self.assertEqual([s["url"] for s in result["sources"]], ["https://example.org/valid"])


if __name__ == "__main__":
    unittest.main()
