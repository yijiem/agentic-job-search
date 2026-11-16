import copy
import unittest

from jobflow.salary_references import DIRECTORY, reference_index, salary_reference


class SalaryReferenceTests(unittest.TestCase):
    def setUp(self):
        self.role_url = 'https://www.levels.fyi/companies/robinhood/salaries/software-engineer'
        self.note = {
            'id': 'levels-robinhood', 'company': 'Robinhood',
            'kind': 'levels_fyi_company_reference', 'observed_at': '2026-11-13',
            'levels_fyi': {
                'company_url': 'https://www.levels.fyi/companies/robinhood/salaries',
                'software_engineer_url': self.role_url,
                'levels': [{'role': 'software-engineer', 'label': 'L3', 'aliases': ['IC5'],
                            'url': self.role_url + '/levels/l3'}],
            },
        }

    def reference(self, title='Senior Software Engineer', **job_fields):
        job = {'company': 'Robinhood', 'title': title, **job_fields}
        return salary_reference(job, reference_index([self.note]))

    def test_seniority_and_experience_do_not_establish_company_grade(self):
        for title in ('Senior Software Engineer', 'Staff Software Engineer', 'Software Engineer III'):
            result = self.reference(title, description='Minimum 5 years; collaborate with IC5 engineers', level='senior')
            self.assertEqual(result['scope'], 'company_role')
            self.assertEqual(result['url'], self.role_url)
            self.assertIsNone(result['level'])
            self.assertIn('unconfirmed', result['note'])

    def test_literal_title_code_uses_verified_level_and_alias(self):
        for title in ('Software Engineer (L3)', 'Software Engineer, IC5'):
            result = self.reference(title)
            self.assertEqual(result['scope'], 'level')
            self.assertEqual(result['url'], self.role_url + '/levels/l3')
            self.assertEqual(result['level'], 'L3')

    def test_ambiguous_or_unknown_codes_use_all_levels(self):
        for title in ('Software Engineer L3/L4', 'Software Engineer P3', 'Software Engineer L2',
                      'Software Engineer, L3 Networking', 'Software Engineer - Layer L3'):
            self.assertEqual(self.reference(title)['scope'], 'company_role')
        self.note['levels_fyi']['levels'].append({
            'role': 'software-engineer', 'label': 'L4', 'aliases': ['IC5'],
            'url': self.role_url + '/levels/l4'})
        self.assertEqual(self.reference('Software Engineer IC5')['scope'], 'company_role')

    def test_unknown_company_and_hostile_urls_fall_back_without_guessing_slug(self):
        self.assertEqual(salary_reference({'company': 'Unknown AI', 'title': 'Engineer'}, {})['url'], DIRECTORY)
        for url in ('https://www.levels.fyi.evil.test/companies/robinhood',
                    'https://user@www.levels.fyi/companies/robinhood',
                    'https://www.levels.fyi:443/companies/robinhood',
                    'https://www.levels.fyi/companies/robin\thood/salaries',
                    'https://www.levels.fyi/companies/robinhood/salaries\n',
                    'https://www.levels.fyi/companies/robinhood?redirect=https://evil.test',
                    'javascript:alert(1)'):
            self.note['levels_fyi']['company_url'] = url
            self.assertEqual(self.reference()['url'], DIRECTORY)

    def test_wrong_company_or_role_path_never_becomes_exact_reference(self):
        self.note['levels_fyi']['levels'][0]['url'] = self.role_url.replace('robinhood', 'twilio') + '/levels/l3'
        self.assertEqual(self.reference('Software Engineer L3')['scope'], 'company_role')
        self.note['levels_fyi']['software_engineer_url'] = self.role_url.replace('software-engineer', 'product-manager')
        self.assertEqual(self.reference()['scope'], 'company')

    def test_other_job_families_get_company_page_and_insufficient_data_note(self):
        self.note['levels_fyi']['salary_data_status'] = 'insufficient'
        for title in ('Product Manager', 'Hardware Engineer', 'Engineering Manager',
                      'Senior Pre-Sales Systems Engineer', 'Thermal Engineer',
                      'Supplier Industrialization Engineering', 'Product Quality Engineer', 'IT Systems Engineer'):
            result = self.reference(title)
            self.assertEqual(result['scope'], 'company')
            self.assertIn('insufficient', result['note'])
        self.assertIn('broader', self.reference('Senior Security Engineer')['note'])

    def test_compensation_and_input_records_are_not_modified(self):
        job = {'company': 'Robinhood', 'title': 'Software Engineer L3',
               'compensation': {'base_min': 100000, 'tc_min': None}, 'score': 45}
        original = copy.deepcopy(job)
        original_note = copy.deepcopy(self.note)
        result = salary_reference(job, reference_index([self.note]))
        self.assertEqual(job, original)
        self.assertEqual(self.note, original_note)
        self.assertNotIn('compensation', result)
        self.assertNotIn('salary', result)

    def test_only_dated_reference_records_build_index_and_newer_wins(self):
        old = copy.deepcopy(self.note)
        old['observed_at'] = '2026-10-01'
        old['levels_fyi']['company_url'] = 'https://www.levels.fyi/companies/old-name'
        unrelated = copy.deepcopy(self.note)
        unrelated.update(kind='unverified_discovery', observed_at='2026-11-14')
        index = reference_index([self.note, unrelated, old])
        self.assertEqual(index['robinhood']['observed_at'], '2026-11-13')
        self.assertEqual(index['robinhood']['company_url'], self.note['levels_fyi']['company_url'])
        undated = copy.deepcopy(self.note)
        del undated['observed_at']
        self.assertEqual(reference_index([undated]), {})


if __name__ == '__main__':
    unittest.main()
