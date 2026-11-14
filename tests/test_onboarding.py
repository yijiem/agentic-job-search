import fcntl
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from jobflow.onboarding import (MAX_UPLOAD_BYTES, initialize_workspace,
                                onboarding_state, save_onboarding, upload_resume)
from jobflow.storage import Store


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'workspace'

    def tearDown(self):
        self.temporary.cleanup()

    def _profile(self):
        return {'name': 'Example Candidate', 'headline': 'Infrastructure engineer',
                'skills': ['Python', 'distributed systems'],
                'evidence': [{'id': 'real-1', 'text': 'Built the documented migration service.',
                              'tags': ['Python'], 'source_url': 'https://example.com/resume'}]}

    def _ready(self):
        initialize_workspace(self.root)
        return save_onboarding(self.root, {'profile': self._profile()})

    def _read(self, name):
        return json.loads((self.root / 'config' / name).read_text())

    def _write(self, name, value):
        (self.root / 'config' / name).write_text(json.dumps(value))

    def test_empty_state_is_read_only_and_initialization_has_no_candidate_evidence(self):
        self.assertFalse(onboarding_state(self.root)['complete'])
        self.assertFalse(self.root.exists())
        state = initialize_workspace(self.root)
        self.assertFalse(state['complete'])
        self.assertEqual(state['profile']['evidence'], [])
        self.assertEqual(state['profile']['skills'], [])
        self.assertEqual(state['profile']['name'], '')
        self.assertEqual(state['source_count'], 0)
        with StoreContext(self.root / 'data/jobflow.sqlite3') as store:
            for kind in ('jobs', 'contacts', 'tasks', 'applications', 'research'):
                self.assertEqual(store.all(kind), [])
        self.assertEqual((self.root / 'config/profile.json').stat().st_mode & 0o777, 0o600)

    def test_existing_profile_skips_onboarding_without_reformatting_or_history_changes(self):
        self._ready()
        profile = self._read('profile.json')
        profile['review_note'] = 'Preserve my note verbatim.'
        profile['evidence'][0]['employer'] = 'Original employer'
        self._write('profile.json', profile)
        with StoreContext(self.root / 'data/jobflow.sqlite3') as store:
            store.put('tasks', 'done-task', {'id': 'done-task', 'status': 'done',
                      'completed_at': '2026-10-01T15:00:00-07:00', 'notes': 'Actual practice'})
            run_id = store.start_run()
            store.finish_run(run_id, 'complete', {'preserved': True})
        paths = [self.root / 'config/profile.json', self.root / 'config/settings.json',
                 self.root / 'data/jobflow.sqlite3']
        before = [path.read_bytes() for path in paths]
        state = onboarding_state(self.root)
        initialized_again = initialize_workspace(self.root, sources=[])
        self.assertTrue(state['complete'])
        self.assertTrue(initialized_again['complete'])
        self.assertTrue(state['existing_data_reused'])
        self.assertEqual(before, [path.read_bytes() for path in paths])

    def test_save_preserves_unknown_config_metadata_and_completed_history(self):
        self._ready()
        settings = self._read('settings.json')
        source = {'id': 'source', 'company': 'Example', 'kind': 'greenhouse',
                  'board': 'example', 'url': 'https://example.com/jobs'}
        settings['sources'] = [source]
        settings['future_setting'] = {'must': 'survive'}
        settings['search']['future_filter'] = 'preserve'
        self._write('settings.json', settings)
        profile = self._read('profile.json')
        profile['review_note'] = 'Preserved note'
        profile['evidence'][0]['employer'] = 'Example'
        profile['evidence'][0]['custom_annotation'] = 'Reviewed by user'
        self._write('profile.json', profile)
        with StoreContext(self.root / 'data/jobflow.sqlite3') as store:
            store.put('tasks', 'task', {'id': 'task', 'status': 'done', 'actual_minutes': 35})
            store.put('applications', 'job', {'id': 'job', 'stage': 'applied',
                      'applied_at': '2026-10-03T10:00:00-07:00'})
            records_before = [tuple(row) for row in store.db.execute('SELECT * FROM records')]
        result = save_onboarding(self.root, {
            'profile': {'headline': 'Updated headline', 'evidence': [
                {'id': 'real-1', 'text': 'Built the documented migration service.'}]},
            'preferences': {'target_tc': 300000, 'daily_minutes': 90,
                            'timezone': 'America/New_York', 'allowed_levels': ['senior', 'staff']}})
        self.assertTrue(result['complete'])
        self.assertEqual(result['preferences']['target_tc'], 300000)
        self.assertEqual(result['preferences']['daily_minutes'], 90)
        updated = self._read('settings.json')
        self.assertEqual(updated['sources'], [source])
        self.assertEqual(updated['future_setting'], {'must': 'survive'})
        self.assertEqual(updated['search']['future_filter'], 'preserve')
        self.assertEqual(updated['preparation']['timezone'], 'America/New_York')
        saved_profile = self._read('profile.json')
        self.assertEqual(saved_profile['review_note'], 'Preserved note')
        self.assertEqual(saved_profile['evidence'][0]['custom_annotation'], 'Reviewed by user')
        self.assertEqual(saved_profile['evidence'][0]['employer'], 'Example')
        with StoreContext(self.root / 'data/jobflow.sqlite3') as store:
            self.assertEqual(records_before, [tuple(row) for row in store.db.execute('SELECT * FROM records')])

    def test_source_template_copies_public_descriptors_only_and_never_replaces_existing(self):
        source = {'id': 'example', 'company': 'Example', 'kind': 'greenhouse',
                  'board': 'example', 'url': 'https://example.com/jobs', 'enabled': True,
                  'private_note': 'not shared', 'credentials': 'not shared'}
        state = initialize_workspace(self.root, sources=[source])
        self.assertEqual(state['source_count'], 1)
        settings = self._read('settings.json')
        self.assertNotIn('private_note', settings['sources'][0])
        self.assertNotIn('credentials', settings['sources'][0])
        initialize_workspace(self.root, sources=[])
        self.assertEqual(self._read('settings.json')['sources'], settings['sources'])

    def test_uploads_never_add_unreviewed_claims_and_preserve_verbatim_text(self):
        self._ready()
        profile_before = (self.root / 'config/profile.json').read_bytes()
        uploaded = upload_resume(self.root, 'my resume.txt',
                                 b'  - Maintained the exact supplied metric: 13%.\nSecond statement.\n')
        self.assertEqual(uploaded['extraction_status'], 'extracted')
        self.assertEqual(uploaded['suggestions'][0]['text'],
                         '  - Maintained the exact supplied metric: 13%.')
        self.assertEqual(profile_before, (self.root / 'config/profile.json').read_bytes())
        self.assertFalse((self.root / 'inputs/resumes/my resume.txt').exists())
        stored = self.root / 'inputs/resumes' / (uploaded['id'] + '.txt')
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        state = onboarding_state(self.root)
        self.assertEqual(state['uploads'][0]['filename'], 'my resume.txt')
        self.assertNotIn('text', state['uploads'][0])
        save_onboarding(self.root, {'profile': {'evidence': uploaded['suggestions']}})
        self.assertEqual(self._read('profile.json')['evidence'][0]['source_file_id'], uploaded['id'])

    def test_cross_workspace_upload_reference_is_rejected(self):
        self._ready()
        other = Path(self.temporary.name) / 'other'
        initialize_workspace(other)
        upload = upload_resume(other, 'resume.txt', b'Private statement for another account.')
        before = (self.root / 'config/profile.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'not in this workspace'):
            save_onboarding(self.root, {'profile': {'evidence': upload['suggestions']}})
        self.assertEqual(before, (self.root / 'config/profile.json').read_bytes())

    def test_pdf_is_retained_with_honest_manual_text_status(self):
        initialize_workspace(self.root)
        result = upload_resume(self.root, 'resume.pdf', b'%PDF-1.4\nminimal test fixture\n%%EOF\n')
        self.assertEqual(result['extraction_status'], 'needs_text')
        self.assertEqual(result['suggestions'], [])
        self.assertEqual(result['text'], '')
        self.assertIn('not configured', result['message'])
        self.assertFalse(onboarding_state(self.root)['complete'])

    def _docx(self, document, extras=None):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('word/document.xml', document)
            for name, content in (extras or {}).items():
                archive.writestr(name, content)
        return data.getvalue()

    def test_docx_extracts_document_text_without_executing_relationships(self):
        initialize_workspace(self.root)
        document = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    '<w:body><w:p><w:r><w:t>Exact achievement</w:t><w:tab/><w:t>13%</w:t></w:r>'
                    '</w:p><w:p><w:r><w:t>Next line</w:t></w:r></w:p></w:body></w:document>')
        result = upload_resume(self.root, 'resume.docx', self._docx(document))
        self.assertEqual(result['text'], 'Exact achievement\t13%\nNext line')
        self.assertEqual(result['suggestions'][0]['text'], 'Exact achievement\t13%')

    def test_malformed_uploads_and_zip_bombs_are_rejected_without_storing_files(self):
        initialize_workspace(self.root)
        invalid = [('../resume.txt', b'x'), ('folder\\resume.txt', b'x'), ('bad\nname.txt', b'x'),
                   ('resume.exe', b'executable'), ('resume.txt', b'\x00binary'),
                   ('resume.txt', b'\xff'), ('resume.docx', b'not a zip'),
                   ('resume.pdf', b'not a PDF'), ('resume.txt', b'x' * (MAX_UPLOAD_BYTES + 1)),
                   ('resume.docx', self._docx('<test/>', {'../outside.txt': 'x'})),
                   ('resume.docx', self._docx('<test/>', {'word/vbaProject.bin': 'macro'})),
                   ('resume.docx', self._docx('<test/>', {'bomb.txt': 'x' * (2 * 1024 * 1024)})),
                   ('resume.docx', self._docx('<!DOCTYPE x [<!ENTITY a "value">]><x>&a;</x>'))]
        for filename, content in invalid:
            with self.subTest(filename=filename, size=len(content)):
                with self.assertRaises(ValueError):
                    upload_resume(self.root, filename, content)
        self.assertFalse((self.root / 'inputs/resumes').exists())

    def test_invalid_preferences_fail_before_either_config_changes(self):
        self._ready()
        before = [(self.root / 'config' / name).read_bytes()
                  for name in ('profile.json', 'settings.json')]
        invalid = [{'daily_minutes': 14}, {'daily_minutes': True}, {'target_tc': float('nan')},
                   {'target_tc': -1}, {'target_tc': True}, {'currency': 'EUR'},
                   {'allowed_levels': ['invented']}, {'timezone': 'Invented/Zone'},
                   {'sources': [{'url': 'https://attacker.invalid'}]},
                   {'schedule': {'days': [True]}}, {'schedule': {'days': [1, 1]}},
                   {'schedule': {'days': [[1]]}},
                   {'schedule': {'hour': 24}}, {'search': {'min_score': 101}},
                   {'daily_goals': {'applications': 200}}]
        for preferences in invalid:
            with self.subTest(preferences=preferences):
                with self.assertRaises(ValueError):
                    save_onboarding(self.root, {'profile': {'headline': 'Should not save'},
                                                'preferences': preferences})
                self.assertEqual(before, [(self.root / 'config' / name).read_bytes()
                                          for name in ('profile.json', 'settings.json')])

    def test_nested_preferences_and_migration_marker_are_preserved(self):
        self._ready()
        self._write('onboarding.json', {'completed': True, 'migrated': True,
                                      'source': 'existing_workspace'})
        result = save_onboarding(self.root, {'preferences': {'search': {'target_tc': 250000},
                                                           'schedule': {'hour': 9}}})
        self.assertEqual(result['preferences']['target_tc'], 250000)
        self.assertEqual(result['preferences']['schedule']['hour'], 9)
        self.assertTrue(result['migrated'])
        self.assertEqual(self._read('onboarding.json')['source'], 'existing_workspace')

    def test_preference_round_trip_preserves_distinct_preparation_timezone(self):
        self._ready()
        settings = self._read('settings.json')
        settings['preparation']['timezone'] = 'Europe/London'
        self._write('settings.json', settings)
        state = onboarding_state(self.root)
        save_onboarding(self.root, {'preferences': state['preferences']})
        self.assertEqual(self._read('settings.json'), settings)
        save_onboarding(self.root, {'preferences': {'timezone': state['preferences']['timezone']}})
        self.assertEqual(self._read('settings.json')['preparation']['timezone'], 'Europe/London')

    def test_lock_blocks_save_and_upload_during_another_workflow(self):
        self._ready()
        before = (self.root / 'config/profile.json').read_bytes()
        with (self.root / 'data/workflow.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'Another workflow'):
                save_onboarding(self.root, {'profile': {'headline': 'Not saved'}})
            with self.assertRaisesRegex(ValueError, 'Another workflow'):
                upload_resume(self.root, 'resume.txt', b'Not saved')
        self.assertEqual(before, (self.root / 'config/profile.json').read_bytes())

    def test_malformed_onboarding_metadata_does_not_partially_save_profile(self):
        self._ready()
        (self.root / 'config/onboarding.json').write_text('{broken')
        before = [(self.root / 'config' / name).read_bytes()
                  for name in ('profile.json', 'settings.json')]
        with self.assertRaises(ValueError):
            save_onboarding(self.root, {'profile': {'headline': 'Must not save'}})
        self.assertEqual(before, [(self.root / 'config' / name).read_bytes()
                                  for name in ('profile.json', 'settings.json')])

    def test_per_account_upload_limits_prevent_unbounded_shared_disk_use(self):
        initialize_workspace(self.root)
        upload_resume(self.root, 'resume.txt', b'An existing statement.')
        files = sorted(path.name for path in (self.root / 'inputs/resumes').iterdir())
        with patch('jobflow.onboarding.MAX_RESUME_UPLOADS', 1):
            with self.assertRaisesRegex(ValueError, 'limit of 1'):
                upload_resume(self.root, 'another.txt', b'Another statement.')
        with patch('jobflow.onboarding.MAX_RESUME_STORAGE_BYTES', 1):
            with self.assertRaisesRegex(ValueError, 'storage limit'):
                upload_resume(self.root, 'another.txt', b'Another statement.')
        self.assertEqual(files, sorted(path.name for path in (self.root / 'inputs/resumes').iterdir()))

    def test_symlink_cannot_redirect_private_configuration_or_uploads(self):
        self._ready()
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir()
        (self.root / 'inputs').mkdir()
        (self.root / 'inputs/resumes').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            upload_resume(self.root, 'resume.txt', b'Private')
        self.assertEqual(list(outside.iterdir()), [])


class StoreContext:
    def __init__(self, path):
        self.store = Store(path)

    def __enter__(self):
        return self.store

    def __exit__(self, *unused):
        self.store.close()


if __name__ == '__main__':
    unittest.main()
