import json
import re
import tempfile
import unittest
from pathlib import Path

from jobflow.reports import write_reports
from jobflow.storage import Store


class CompanyReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite3")
        self.config = {"search": {"min_score": 40, "target_tc": 500000}}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add_job(self, key, company="Databricks", title="Software Engineer",
                score=60, eligible=True, availability="open", **extra):
        job = {
            "id": key, "company": company, "title": title,
            "url": "https://example.org/jobs/" + key,
            "score": score, "eligible": eligible, "availability": availability,
            "location": "San Francisco, CA", "description": "Example role",
            "compensation_status": "unverified_base_only",
            "compensation": {"base_min": 100000, "base_max": 200000,
                             "currency": "USD", "tc_status": "base_only"},
        }
        job.update(extra)
        self.store.put("jobs", key, job)
        return job

    def render(self):
        return write_reports(self.root, self.store, self.config).read_text()

    def company_links(self, review):
        # Inspect the company index, not differently labelled links elsewhere.
        index = self.section(review, "Openings by company")
        links = {}
        for company, target in re.findall(r"\[([^\]]+)\]\((companies/[^)]+\.md)\)", index):
            links.setdefault(company, target)
        return links

    def section(self, text, title):
        heading = re.search(r"^(#{1,6})\s+" + re.escape(title) + r"\s*$", text, re.MULTILINE)
        self.assertIsNotNone(heading, "Missing section: " + title)
        rest = text[heading.end():]
        following = re.search(r"^#{1," + str(len(heading.group(1))) + r"}\s+", rest, re.MULTILINE)
        return rest[:following.start()] if following else rest

    def test_company_browsing_includes_every_match_and_sorts_within_company(self):
        # More than 100 matches in one company catches both global and local caps.
        expected = []
        for number in range(105):
            expected.append(self.add_job("db-%03d" % number, title="Engineer %03d" % number))
        expected.append(self.add_job("rank-zulu", title="Zulu Engineer", score=90))
        expected.append(self.add_job("rank-alpha", title="Alpha Engineer", score=90))
        # Insert companies out of alphabetical order and give them different scores.
        self.add_job("zeta-role", company="Zeta", score=100)
        self.add_job("alpha-role", company="Alpha", score=40)

        review = self.render()
        self.assertIn("Openings by company", review)
        links = self.company_links(review)
        self.assertEqual(["Alpha", "Databricks", "Zeta"], list(links))
        self.assertEqual("companies/databricks.md", links["Databricks"])
        page = (self.root / "reports" / links["Databricks"]).read_text()
        matching = self.section(page, "Matching your preferences")
        expected.sort(key=lambda job: (-job["score"], job["title"]))
        positions = []
        for job in expected:
            self.assertIn(job["id"], review)
            self.assertIn(job["id"], matching)
            positions.append(matching.index(job["id"]))
        self.assertEqual(sorted(positions), positions)

    def test_company_views_show_only_matches_and_flag_uncertain_availability(self):
        self.add_job("matched-open", score=80)
        self.add_job("ineligible-open", score=100, eligible=False)
        self.add_job("below-threshold-open", score=39)
        self.add_job("missing-role", score=95, availability="missing")
        self.add_job("unknown-role", score=95, availability="unverified")
        self.add_job("no-availability-role", score=95, availability=None)
        self.add_job("closed-role", score=99, availability="closed")
        self.add_job("unmatched-company-role", company="Unmatched Company", eligible=False)

        review = self.render()
        page = (self.root / "reports/companies/databricks.md").read_text()
        self.assertEqual(["Databricks"], list(self.company_links(review)))
        expected_ids = {"matched-open", "missing-role", "unknown-role", "no-availability-role"}
        for content in (review, page):
            for key in expected_ids:
                self.assertIn(key, content)
            for key in ("ineligible-open", "below-threshold-open", "closed-role", "unmatched-company-role"):
                self.assertNotIn(key, content)
            self.assertNotIn("Other open roles", content)
            self.assertNotIn("Needs rechecking", content)
            self.assertIn("recheck", content.lower())
            missing_row = next(line for line in content.splitlines() if "missing-role" in line)
            self.assertRegex(missing_row, r"\|\s*missing\s*\|")
        for company_page in (self.root / "reports/companies").glob("*.md"):
            self.assertNotIn("unmatched-company-role", company_page.read_text())
        exported = json.loads((self.root / "reports/jobs.json").read_text())
        self.assertEqual("closed", next(job for job in exported if job["id"] == "closed-role")["availability"])
        company_row = next(line for line in review.splitlines()
                           if "[Databricks](companies/databricks.md)" in line and line.startswith("|"))
        counts = [int(cell.strip()) for cell in company_row.split("|") if cell.strip().isdigit()]
        self.assertEqual([4], counts)

    def test_company_page_is_cleared_when_its_last_role_stops_matching(self):
        job = self.add_job("formerly-matched-role", company="Former Match")
        first_links = self.company_links(self.render())
        page_path = self.root / "reports" / first_links["Former Match"]
        self.assertIn(job["id"], page_path.read_text())
        # Simulate a page generated by the former all-open-role report.
        page_path.write_text(page_path.read_text() + "\n## Other open roles\n\nlegacy-unmatched-role\n")
        job["eligible"] = False
        self.store.put("jobs", job["id"], job)

        review = self.render()

        self.assertNotIn("Former Match", self.company_links(review))
        self.assertNotIn(job["id"], review)
        self.assertTrue(page_path.is_file())
        refreshed = page_path.read_text()
        self.assertNotIn(job["id"], refreshed)
        self.assertNotIn("legacy-unmatched-role", refreshed)
        self.assertNotIn("Other open roles", refreshed)

    def test_review_queue_only_links_matching_packets_but_keeps_historical_exports(self):
        matched = self.add_job("packet-match")
        missing = self.add_job("packet-missing", availability="missing")
        ineligible = self.add_job("packet-ineligible", score=100, eligible=False)
        closed = self.add_job("packet-closed", availability="closed")
        jobs = (matched, missing, ineligible, closed)
        for job in jobs:
            self.store.put("packets", job["id"], {
                "id": job["id"], "job_id": job["id"], "company": job["company"],
                "title": job["title"], "markdown": "Packet content: " + job["id"],
                "resume_markdown": "Resume content: " + job["id"],
            })

        review = self.render()

        queue = self.section(review, "Review queue")
        for job in (matched, missing):
            self.assertIn("packets/" + job["id"] + ".md", queue)
        for job in (ineligible, closed):
            self.assertNotIn(job["id"], review)
        for job in jobs:
            key = job["id"]
            self.assertEqual("Packet content: " + key,
                             (self.root / "reports/packets" / (key + ".md")).read_text())
            self.assertEqual("Resume content: " + key,
                             (self.root / "reports/resumes" / (key + ".md")).read_text())

    def test_company_filename_collisions_do_not_overwrite_or_escape_reports(self):
        companies = ["A&B", "A B", "a-b", "../../Outside", "Databricks"]
        keys = {}
        for number, company in enumerate(companies):
            keys[company] = "company-role-%s" % number
            self.add_job(keys[company], company=company)

        first_links = self.company_links(self.render())
        self.assertEqual(set(companies), set(first_links))
        self.assertEqual(len(companies), len(set(first_links.values())))
        report_directory = (self.root / "reports/companies").resolve()
        for company, target in first_links.items():
            page = (self.root / "reports" / target).resolve()
            self.assertEqual(report_directory, page.parent)
            self.assertTrue(page.is_file())
            content = page.read_text()
            self.assertIn(keys[company], content)
            for other, key in keys.items():
                if other != company:
                    self.assertNotIn(key, content)
        self.assertEqual(first_links, self.company_links(self.render()))

    def test_company_reports_preserve_tracking_records_and_export_complete_job_history(self):
        first = self.add_job("same-title-one", title="Staff Engineer", location="San Francisco, CA")
        second = self.add_job("same-title-two", title="Staff Engineer", location="San Mateo, CA")
        excluded = self.add_job("unmatched-history", location="London, UK", eligible=False)
        closed = self.add_job("historical-role", availability="closed")
        application = {"id": first["id"], "job_id": first["id"], "stage": "applied",
                       "notes": "Keep my application notes"}
        task = {"id": "finished-task", "status": "done", "title": "My edited title",
                "notes": "Keep my retrospective", "due_date": "2026-10-01",
                "history": [{"status": "done", "at": "2026-10-01T18:00:00+00:00"}]}
        self.store.put("applications", application["id"], application)
        self.store.put("tasks", task["id"], task)
        before = [tuple(row) for row in self.store.db.execute("SELECT * FROM records ORDER BY kind,id")]

        self.render()

        after = [tuple(row) for row in self.store.db.execute("SELECT * FROM records ORDER BY kind,id")]
        self.assertEqual(before, after)
        page = (self.root / "reports/companies/databricks.md").read_text()
        self.assertIn(first["id"], page)
        self.assertIn(second["id"], page)
        self.assertIn("San Francisco, CA", page)
        self.assertIn("San Mateo, CA", page)
        self.assertNotIn(excluded["id"], page)
        self.assertNotIn("London, UK", page)
        exported = json.loads((self.root / "reports/jobs.json").read_text())
        for job in exported:
            self.assertIn('levels_fyi', job)
            self.assertEqual(job['levels_fyi']['scope'], 'directory')
        self.assertEqual({job["id"]: job for job in (first, second, excluded, closed)},
                         {job["id"]: {k: v for k, v in job.items() if k != 'levels_fyi'} for job in exported})

    def test_salary_links_in_company_review_and_json_use_saved_references(self):
        job = self.add_job('robinhood-l3', company='Robinhood', title='Software Engineer (L3)')
        url = 'https://www.levels.fyi/companies/robinhood/salaries/software-engineer'
        self.store.put('research', 'salary-reference', {
            'id': 'salary-reference', 'company': 'Robinhood', 'kind': 'levels_fyi_company_reference',
            'observed_at': '2026-11-13', 'text': 'Verified public salary reference',
            'sources': [{'url': url, 'title': 'Levels.fyi'}],
            'levels_fyi': {'company_url': 'https://www.levels.fyi/companies/robinhood/salaries',
                          'software_engineer_url': url,
                          'levels': [{'role': 'software-engineer', 'label': 'L3',
                                      'url': url + '/levels/l3'}]},
        })
        review = self.render()
        company = (self.root / 'reports/companies/robinhood.md').read_text()
        for content in (review, company):
            self.assertIn('Levels.fyi salary reference', content)
            self.assertIn(url + '/levels/l3', content)
        saved = self.store.get('jobs', job['id'])
        exported = json.loads((self.root / 'reports/jobs.json').read_text())[0]
        self.assertNotIn('levels_fyi', saved)
        self.assertEqual(exported['levels_fyi']['scope'], 'level')
        self.assertEqual(exported['compensation'], saved['compensation'])


if __name__ == "__main__":
    unittest.main()
