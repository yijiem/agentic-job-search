import copy
import json
import unittest

from jobflow.service_context import service_catalog


class ServiceContextTests(unittest.TestCase):
    def observation(self, **changes):
        return {"id": "lc-observation", "kind": "service_context", "service": "leetcode",
                "observed_at": "2026-11-14", "browser_access": "signed_in", "account_handle": "example_user",
                "profile_url": "https://leetcode.com/u/example_user/",
                "text": "Original research data is not copied to the projection.",
                "sources": [{"url": "https://leetcode.com/u/example_user/", "title": "Profile"}], **changes}

    def leetcode(self, records):
        return next(row for row in service_catalog(records) if row["id"] == "leetcode")

    def test_empty_catalog_does_not_claim_a_connection_or_known_browser_status(self):
        result = service_catalog([])
        self.assertEqual([r["id"] for r in result], ["leetcode", "teamblind", "1point3acres", "linkedin"])
        for row in result:
            self.assertEqual(row["mode"], "browser_assisted")
            self.assertEqual(row["connection_status"], "not_connected")
            self.assertEqual(row["last_observed_at"], "")
            self.assertNotIn("access_status", row)
            self.assertEqual(row["context"], [])
            self.assertIn("Google Sign-In", row["capability_note"])

    def test_signed_in_is_a_dated_observation_not_api_authorization(self):
        row = self.leetcode([self.observation(metrics={"solved_total": 120, "solved_easy": 30})])
        self.assertEqual(row["access_status"], "signed_in")
        self.assertEqual(row["connection_status"], "not_connected")
        self.assertEqual(row["account_handle"], "example_user")
        self.assertEqual(row["last_observed_at"], "2026-11-14")
        self.assertEqual(row["context"][0]["metrics"], {"solved_total": 120, "solved_easy": 30})
        self.assertIn("was observed", row["context"][0]["text"])

    def test_latest_observation_wins_and_missing_fields_do_not_inherit_old_identity(self):
        older = self.observation(observed_at="2026-11-13T23:00:00-08:00")
        newer = self.observation(id="new", observed_at="2026-11-14T09:00:00Z",
                                 browser_access="sign_in_required", account_handle="", profile_url="")
        row = self.leetcode([newer, older])
        self.assertEqual(row["access_status"], "sign_in_required")
        self.assertNotIn("account_handle", row)
        self.assertNotIn("profile_url", row)
        self.assertEqual(len(row["context"]), 2)
        self.assertEqual(row, self.leetcode([older, newer]))

    def test_credentials_and_arbitrary_metadata_are_never_projected(self):
        record = self.observation(password="secret-password", token="secret-token",
                                  cookies={"session": "secret-cookie"}, text="secret-in-text",
                                  metrics={"solved_total": 4, "token": "secret-metric", "solved_easy": True,
                                           "solved_medium": -3, "solved_hard": "unknown"})
        result = self.leetcode([record])
        serialized = json.dumps(result)
        for value in ("secret-password", "secret-token", "secret-cookie", "secret-in-text", "secret-metric"):
            self.assertNotIn(value, serialized)
        self.assertEqual(result["context"][0]["metrics"], {"solved_total": 4})

    def test_provider_links_reject_credentials_and_other_hosts(self):
        bad_urls = ["javascript:alert(1)", "https://leetcode.com.evil.example/u/person/",
                    "https://elsewhere.example/", "https://user:password@leetcode.com/u/person/",
                    "https://leetcode.com/u/person/?token=secret", "https://leetcode.com/u/person/#token=secret",
                    "https://leetcode.com:8443/u/person/", "https://leetcode.com/\nother"]
        for url in bad_urls:
            with self.subTest(url=url):
                self.assertEqual(self.leetcode([self.observation(sources=[{"url": url}])])["context"], [])
                result = self.leetcode([self.observation(profile_url=url)])
                self.assertNotIn("profile_url", result)

    def test_invalid_dates_wrong_services_and_coding_progress_are_not_account_snapshots(self):
        records = [self.observation(observed_at="not-a-date"),
                   self.observation(observed_at="2026-11-14T09:00:00"),
                   self.observation(service="unknown"), self.observation(kind="coding_progress"),
                   self.observation(sources=[])]
        self.assertEqual(self.leetcode(records)["context"], [])

    def test_browser_access_enumeration_and_handles_are_validated(self):
        result = self.leetcode([self.observation(browser_access="connected", account_handle="person@example.com")])
        self.assertNotIn("access_status", result)
        self.assertNotIn("account_handle", result)
        self.assertIn("not established", result["context"][0]["text"])
        malformed = self.leetcode([self.observation(browser_access={"token": "secret"}, metrics=None)])
        self.assertNotIn("access_status", malformed)
        self.assertNotIn("metrics", malformed["context"][0])

    def test_inputs_and_returned_catalog_do_not_share_mutable_state(self):
        record = self.observation(metrics={"total_solved": 42})
        before = copy.deepcopy(record)
        first = service_catalog([record])
        self.assertEqual(record, before)
        first[0]["context"][0]["metrics"]["total_solved"] = 99
        first[0]["documentation_sources"][0]["title"] = "Changed"
        self.assertEqual(record, before)
        self.assertNotEqual(first, service_catalog([record]))

    def test_linkedin_explicit_professional_summary_is_preserved_as_inert_bounded_text(self):
        record = self.observation(service="linkedin", account_handle="example",
                                  profile_url="https://www.linkedin.com/in/example/",
                                  sources=[{"url": "https://www.linkedin.com/in/example/"}],
                                  profile_summary="Google API serving; gRPC EventEngine and OpenTelemetry. "
                                                  "NetApp FUSE and RocksDB projects.",
                                  text="Raw note not exposed", password="Private credential")
        before = copy.deepcopy(record)
        result = next(row for row in service_catalog([record]) if row["id"] == "linkedin")
        context = result["context"][0]
        self.assertEqual(context["profile_summary"], record["profile_summary"])
        self.assertIn(record["profile_summary"], context["text"])
        self.assertNotIn("Raw note not exposed", context["text"])
        self.assertNotIn("Private credential", json.dumps(result))
        self.assertEqual(record, before)
        record["profile_summary"] = "x" * 3000
        result = next(row for row in service_catalog([record]) if row["id"] == "linkedin")
        self.assertEqual(len(result["context"][0]["profile_summary"]), 2000)

    def test_professional_summary_requires_linkedin_and_valid_text(self):
        result = self.leetcode([self.observation(profile_summary="Not a LinkedIn observation")])
        self.assertNotIn("profile_summary", result["context"][0])
        for summary in ({"token": "private"}, "Control\x00character", "   "):
            record = self.observation(service="linkedin", profile_summary=summary,
                                      sources=[{"url": "https://www.linkedin.com/in/example/"}])
            result = next(row for row in service_catalog([record]) if row["id"] == "linkedin")
            self.assertNotIn("profile_summary", result["context"][0])


if __name__ == "__main__":
    unittest.main()
