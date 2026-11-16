import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from jobflow.resume_ai import capabilities, suggest_edits, _utf16_len


def document(text, pages=None):
    lines, offset = [], 0
    for index, line in enumerate(text.split('\n')):
        if line:
            lines.append({'text': line, 'start': offset, 'end': offset + _utf16_len(line),
                          'page': pages[index] if pages else 1})
        offset += _utf16_len(line) + 1
    return {'text': text, 'lines': lines}


class ResumeAITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.job = {'id': 'role', 'title': 'Python Engineer', 'description': 'Build Python services with Rust.',
                    'url': 'https://jobs.example.com/role'}
        self.profile = {'skills': ['Python', 'Rust', 'C++']}
        self.environment = patch.dict(os.environ, {'OPENAI_API_KEY': 'fake-secret-never-print',
                                                   'OPENAI_MODEL': 'configured-test-model'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.doc = document('Ada Example 😀\nExperience\n- Built Python services, improving latency by 20%.\n- Maintained C++ tools.')

    def edit(self, **changes):
        row = {'start_line_id': 2, 'end_line_id': 2,
               'replacement': '- Built Python services; improved latency by 20%.',
               'reason': 'Make the supported performance outcome easier to scan.',
               'evidence_quotes': ['Built Python services, improving latency by 20%.']}
        row.update(changes)
        return row

    def response(self, edits=None, **changes):
        value = {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps({'edits': [self.edit()] if edits is None else edits})}]}],
            'usage': {'input_tokens': 20, 'output_tokens': 5, 'total_tokens': 25, 'private': 'omit'}}
        value.update(changes)
        return io.BytesIO(json.dumps(value).encode())

    def call(self, **kwargs):
        return suggest_edits(self.root, self.doc, self.job, self.profile, mode='ai', **kwargs)

    def test_capabilities_and_hosted_guard_prevent_owner_key_use(self):
        info = capabilities(self.root)
        self.assertTrue(info['ai_available'])
        self.assertEqual(info['model'], 'configured-test-model')
        self.assertNotIn('fake-secret', json.dumps(info))
        (self.root / 'config').mkdir()
        (self.root / 'config/hosted.json').write_text('{}')
        with patch('jobflow.resume_ai.urllib.request.urlopen') as request:
            self.assertFalse(capabilities(self.root)['ai_available'])
            with self.assertRaisesRegex(ValueError, 'hosted'):
                self.call()
            request.assert_not_called()

    def test_missing_model_or_key_and_disabled_flag_fail_before_network(self):
        with patch.dict(os.environ, {}, clear=True), patch('jobflow.resume_ai.urllib.request.urlopen') as request:
            self.assertFalse(capabilities(self.root)['ai_available'])
            with self.assertRaisesRegex(ValueError, 'OPENAI_MODEL'):
                self.call()
            request.assert_not_called()

    def test_capability_reads_bounded_local_settings_and_refuses_symlinks(self):
        (self.root / 'config').mkdir()
        path = self.root / 'config/settings.json'
        path.write_text(json.dumps({'resume_ai': {'enabled': False}}))
        self.assertFalse(capabilities(self.root)['ai_available'])
        path.unlink()
        external = self.root / 'settings-elsewhere.json'
        external.write_text('{}')
        path.symlink_to(external)
        self.assertFalse(capabilities(self.root)['ai_available'])
        with patch('jobflow.resume_ai.urllib.request.urlopen') as request:
            with self.assertRaisesRegex(ValueError, 'disabled'):
                self.call(config={'resume_ai': {'enabled': False}})
            request.assert_not_called()

    def test_local_reorders_exact_claims_without_network_and_preserves_utf16(self):
        self.doc = document('Ada 😀\nExperience\n- Maintained C++ tools.\n- Built Python services.')
        with patch('jobflow.resume_ai.urllib.request.urlopen') as request:
            result = suggest_edits(self.root, self.doc, self.job, self.profile)
            request.assert_not_called()
        self.assertEqual(result['method'], 'local')
        self.assertEqual(len(result['suggestions']), 1)
        edit = result['suggestions'][0]
        self.assertEqual(edit['start'], _utf16_len('Ada 😀\nExperience\n'))
        self.assertEqual(sorted(edit['before'].splitlines()), sorted(edit['after'].splitlines()))
        self.assertTrue(edit['after'].startswith('- Built Python'))

    def test_local_skills_reorder_and_noop_are_honest(self):
        source = document('Ada\nSkills\nC++, Rust, Python\nExperience\nBuilt services.')
        result = suggest_edits(self.root, source, self.job, self.profile)
        self.assertEqual(result['suggestions'][0]['after'], 'Rust, Python, C++\n')
        no_op = suggest_edits(self.root, document('Ada\nSkills\nPython, Rust'), self.job, self.profile)
        self.assertEqual(no_op['suggestions'], [])
        self.assertIn('No safe', no_op['message'])

    def test_ai_request_is_bounded_structured_and_uses_configured_model(self):
        with patch('jobflow.resume_ai.urllib.request.urlopen', return_value=self.response()) as request:
            result = self.call(config={'resume_ai': {'max_output_tokens': 1000}})
        request.assert_called_once()
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual(body['model'], 'configured-test-model')
        self.assertEqual(body['max_output_tokens'], 1000)
        self.assertFalse(body['store'])
        self.assertEqual(body['text']['format']['type'], 'json_schema')
        self.assertTrue(body['text']['format']['strict'])
        self.assertNotIn('tools', body)
        self.assertIn('untrusted data', body['instructions'])
        self.assertEqual(result['suggestions'][0]['start'], self.doc['lines'][2]['start'])
        self.assertEqual(result['suggestions'][0]['source'], 'ai')
        self.assertNotIn('private', result['usage'])
        self.assertNotIn('fake-secret', json.dumps(result))

    def test_model_metrics_evidence_unknown_positions_and_overlap_are_rejected(self):
        cases = [
            [self.edit(replacement='- Built Python services; improved latency by 200%.')],
            [self.edit(evidence_quotes=['Claim not present'])],
            [self.edit(start_line_id=-1)],
            [self.edit(start_line_id=True)],
            [self.edit(), self.edit()],
        ]
        for edits in cases:
            with self.subTest(edits=edits), patch('jobflow.resume_ai.urllib.request.urlopen', return_value=self.response(edits)):
                with self.assertRaises(ValueError):
                    self.call()

    def test_cross_page_model_suggestion_is_rejected(self):
        self.doc['lines'][3]['page'] = 2
        with patch('jobflow.resume_ai.urllib.request.urlopen', return_value=self.response([self.edit(end_line_id=3)])):
            with self.assertRaisesRegex(ValueError, 'multiple pages'):
                self.call()

    def test_malformed_or_incomplete_responses_and_refusal_fail_safely(self):
        changes = [
            {'status': 'incomplete'}, {'output': []}, {'output': [None]},
            {'output': [{'content': None}]},
            {'output': [{'content': [{'type': 'refusal', 'refusal': 'declined'}]}]},
            {'output': [{'content': [{'type': 'output_text', 'text': 'not JSON'}]}]},
        ]
        for change in changes:
            with self.subTest(change=change), patch('jobflow.resume_ai.urllib.request.urlopen', return_value=self.response(**change)):
                with self.assertRaises(ValueError):
                    self.call()

    def test_api_failures_never_leak_key_or_retry(self):
        error = HTTPError('https://api.openai.com/v1/responses', 401, 'fake-secret-never-print', {}, None)
        with patch('jobflow.resume_ai.urllib.request.urlopen', side_effect=error) as request:
            with self.assertRaises(ValueError) as caught:
                self.call()
            request.assert_called_once()
        self.assertNotIn('fake-secret', str(caught.exception))
        self.assertIn('HTTP 401', str(caught.exception))

    def test_stale_line_data_or_surrogate_boundary_rejected_before_network(self):
        bad = document('😀 text')
        bad['lines'][0].update(start=1, text='text')
        with patch('jobflow.resume_ai.urllib.request.urlopen') as request:
            with self.assertRaises(ValueError):
                suggest_edits(self.root, bad, self.job, self.profile, mode='ai')
            self.doc['lines'][2]['text'] = 'Unexpected change'
            with self.assertRaises(ValueError):
                self.call()
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
