import unittest

from jobflow.matching import match_job


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"skills": ["C++", "Go", "Python", "gRPC", "GKE", "OpenTelemetry", "RDMA", "networking", "distributed systems", "storage"]}
        self.config = {"search": {"location_mode": "bay_area_remote", "target_tc": 500000, "min_score": 35,
                                  "allowed_levels": ["mid", "senior", "staff"]}}
        self.job = {"title": "Staff Software Engineer, Infrastructure", "company": "A", "location": "San Francisco, CA",
                    "description": "Distributed systems, C++, Python, Go, gRPC and RDMA networking. Kubernetes and OTel.",
                    "compensation": {}, "remote": False}

    def match(self, **kwargs):
        return match_job(dict(self.job, **kwargs), self.profile, self.config)

    def test_strong_match_without_tc_remains_reviewable(self):
        result = self.match()
        self.assertTrue(result["eligible"])
        self.assertGreaterEqual(result["score"], 80)
        self.assertIn("C++", result["matched_skills"])
        self.assertIn("Go", result["matched_skills"])
        self.assertEqual(result["compensation_status"], "unknown")
        self.assertTrue(any("compensation" in gap for gap in result["gaps"]))

    def test_excluded_levels_and_non_engineering(self):
        for title in ("Software Engineer Intern", "Software Engineer, Early Career", "Junior Software Engineer", "Principal Software Engineer",
                      "Director of Engineering", "Software Engineering Manager", "Software Engineer I", "Data Scientist", "Senior Solutions Engineer",
                      "Staff Technical Recruiter", "Senior Hardware Engineer", "Account Executive"):
            with self.subTest(title=title):
                self.assertFalse(self.match(title=title)["eligible"])

    def test_allowed_levels_and_unlabeled_software_roles(self):
        for title in ("Software Engineer II", "Senior Software Engineer", "Staff Software Engineer", "Software Engineer", "Member of Technical Staff",
                      "Senior Site Reliability Engineer", "Research Engineer, Infrastructure"):
            with self.subTest(title=title):
                self.assertTrue(self.match(title=title)["eligible"])

    def test_allowed_levels_config_applied(self):
        self.config["search"]["allowed_levels"] = ["mid", "senior"]
        self.assertFalse(self.match()["eligible"])

    def test_employment_intern_not_accepted(self):
        self.assertFalse(self.match(employment_type="Intern")["eligible"])

    def test_bay_area_and_multi_location_allowed(self):
        for location in ("San Mateo, CA, United States", "Mountain View, California", "London; San Francisco", "Menlo Park", "Redwood City"):
            with self.subTest(location=location):
                self.assertTrue(self.match(location=location)["eligible"])

    def test_us_remote_allowed(self):
        for location in ("Remote - United States", "US (Remote)", "Remote, California", "Remote, Seattle, WA", "United States"):
            with self.subTest(location=location):
                self.assertTrue(self.match(location=location, remote=True)["eligible"])

    def test_foreign_remote_excluded(self):
        for location in ("Remote - Canada", "Remote - United Kingdom", "London (Remote)", "India", "Remote - EU", "Remote - EMEA", "Toronto, Canada"):
            with self.subTest(location=location):
                self.assertFalse(self.match(location=location, remote=True)["eligible"])

    def test_unknown_remote_us_eligibility_not_assumed(self):
        result = self.match(location="Remote", remote=True, description="Build C++. US employees receive benefits.")
        self.assertFalse(result["eligible"])
        self.assertTrue(any("US" in gap for gap in result["gaps"]))

    def test_explicit_us_remote_description_allowed(self):
        self.assertTrue(self.match(location="Remote", remote=True, description="This is a remote role within the United States. Build Python.")["eligible"])

    def test_non_bay_onsite_excluded(self):
        for location in ("Seattle, WA", "New York, NY", "Austin, Texas", "United States", "California"):
            with self.subTest(location=location):
                self.assertFalse(self.match(location=location)["eligible"])

    def test_hybrid_is_not_us_remote(self):
        self.assertFalse(self.match(location="New York, NY", remote=True, workplace_type="Hybrid")["eligible"])

    def test_base_never_estimated_as_tc(self):
        result = self.match(compensation={"base_min": 300000, "base_max": 700000, "currency": "USD", "tc_min": None, "tc_max": None})
        self.assertTrue(result["eligible"])
        self.assertEqual(result["compensation_status"], "unverified_base_only")
        self.assertFalse(any("meets" in reason for reason in result["reasons"]))

    def test_explicit_tc_status(self):
        for minimum, maximum, expected in ((500000, 700000, "target_met"), (400000, 600000, "target_possible"), (200000, 300000, "below_target")):
            with self.subTest(status=expected):
                result = self.match(compensation={"tc_min": minimum, "tc_max": maximum, "currency": "USD"})
                self.assertEqual(result["compensation_status"], expected)

    def test_non_usd_not_treated_as_usd(self):
        result = self.match(compensation={"tc_min": 600000, "tc_max": 800000, "currency": "CAD"})
        self.assertEqual(result["compensation_status"], "non_usd")

    def test_go_verb_not_a_skill(self):
        result = self.match(description="We go above and beyond to improve products.")
        self.assertNotIn("Go", result["matched_skills"])

    def test_skills_must_exist_in_profile(self):
        self.profile["skills"] = ["Rust"]
        self.assertEqual(self.match()["matched_skills"], [])

    def test_score_bounded_and_threshold_applied(self):
        self.config["search"]["min_score"] = 101
        result = self.match()
        self.assertFalse(result["eligible"])
        self.assertLessEqual(result["score"], 100)


if __name__ == "__main__":
    unittest.main()
