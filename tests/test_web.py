import gzip
import json
import shutil
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jobflow.storage import Store
from jobflow.web import (JobflowServer, WebError, _run_cli, job_detail,
                         perform_action, profile_summary, snapshot)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        project = Path(__file__).resolve().parents[1]
        for name in ('settings', 'profile'):
            shutil.copyfile(project / 'config' / (name + '.example.json'),
                            self.root / 'config' / (name + '.json'))
        self.store = Store(self.root / 'data/jobflow.sqlite3')
        self.job = {'id': 'job-1', 'company': 'Example', 'title': 'Senior Software Engineer',
                    'location': 'San Francisco', 'url': 'https://example.com/job',
                    'description': '<script>untrusted()</script> Build systems.', 'score': 90,
                    'eligible': True, 'availability': 'missing',
                    'first_seen': '2026-11-14T01:00:00+00:00', 'last_seen': '2026-11-14T17:00:00+00:00',
                    'compensation': {'base_min': 200000, 'base_max': 250000, 'tc_status': 'unknown'},
                    'matched_skills': ['C++'], 'reasons': [], 'gaps': []}
        self.store.put('jobs', self.job['id'], self.job)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_snapshot_local_dates_history_and_compensation_are_truthful(self):
        self.store.put('jobs', 'hidden', dict(self.job, id='hidden', eligible=False, score=10))
        self.store.put('jobs', 'closed', dict(self.job, id='closed', availability='closed'))
        result = snapshot(self.root)
        self.assertEqual(result['totals'], {'stored_jobs': 3, 'visible_jobs': 2, 'matches': 1})
        row = next(j for j in result['jobs'] if j['id'] == 'job-1')
        self.assertEqual(row['first_seen_date'], '2026-11-13')
        self.assertEqual(row['availability'], 'missing')
        self.assertNotIn('description', row)
        self.assertEqual(row['compensation']['tc_status'], 'unknown')
        self.assertNotIn('tc_max', row['compensation'])
        self.assertEqual(result['history_kind'], 'cumulative_first_discoveries')
        self.assertEqual(result['history'][0]['companies'], {'Example': 2})
        self.assertNotIn('profile', result)
        self.assertNotIn('sources', result['settings'])
        self.assertEqual(job_detail(self.root, 'job-1')['job']['description'], self.job['description'])

    def test_empty_workspace_read_creates_nothing(self):
        with tempfile.TemporaryDirectory() as empty:
            result = snapshot(empty)
            self.assertFalse(result['initialized'])
            self.assertEqual(result['jobs'], [])
            self.assertEqual(list(Path(empty).iterdir()), [])

    def test_profile_summary_is_read_only_and_allowlists_nested_evidence(self):
        path = self.root / 'config/profile.json'
        profile = {'name': 'Example Candidate', 'headline': 'Systems engineer',
                   'skills': ['Python', 'C++'], 'resume_sources': ['/private/resume.txt'],
                   'private_note': 'private top-level metadata',
                   'evidence': [{'id': 'achievement', 'text': 'Built a system.',
                                 'tags': ['Python'], 'employer': 'Example',
                                 'source_url': 'https://example.com/resume',
                                 'source_path': '/private/source',
                                 'private_note': 'private nested metadata'},
                                {'id': 'local', 'text': 'Reviewed local source.',
                                 'source_url': 'file:///private/resume.txt'}]}
        path.write_text(json.dumps(profile))
        before_profile = path.read_bytes()
        before_records = self.store.db.execute('SELECT * FROM records ORDER BY kind,id').fetchall()
        result = profile_summary(self.root)['profile']
        self.assertEqual(result['name'], profile['name'])
        self.assertEqual(result['headline'], profile['headline'])
        self.assertEqual(result['skills'], profile['skills'])
        self.assertEqual(result['evidence'][0], {
            'id': 'achievement', 'text': 'Built a system.', 'tags': ['Python'],
            'employer': 'Example', 'source_url': 'https://example.com/resume'})
        self.assertNotIn('source_url', result['evidence'][1])
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(path.read_bytes(), before_profile)
        self.assertEqual(self.store.db.execute('SELECT * FROM records ORDER BY kind,id').fetchall(), before_records)

    def test_profile_summary_missing_or_malformed_optional_fields_have_empty_defaults(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(profile_summary(empty), {
                'profile': {'name': '', 'headline': '', 'skills': [], 'evidence': []}})
            self.assertEqual(list(Path(empty).iterdir()), [])
        path = self.root / 'config/profile.json'
        path.write_text(json.dumps({'name': None, 'headline': [], 'skills': None,
                                    'evidence': ['unsupported', {'text': 'Recorded', 'tags': None}]}))
        self.assertEqual(profile_summary(self.root), {'profile': {
            'name': '', 'headline': '', 'skills': [], 'evidence': [{'text': 'Recorded', 'tags': []}]}})

    def test_tracked_nonmatch_is_visible(self):
        self.store.put('jobs', 'historical', dict(self.job, id='historical', eligible=False, score=0))
        self.store.put('applications', 'historical', {'id': 'historical', 'job_id': 'historical', 'stage': 'applied'})
        self.assertIn('historical', [j['id'] for j in snapshot(self.root)['jobs']])

    def test_discovery_history_excludes_tracked_nonmatches_but_retains_closed_matches(self):
        for identifier, fields in (
                ('ineligible', {'eligible': False, 'score': 90}),
                ('below-threshold', {'eligible': True, 'score': 0}),
                ('closed-match', {'eligible': True, 'score': 90, 'availability': 'closed'})):
            self.store.put('jobs', identifier, dict(self.job, id=identifier, **fields))
            self.store.put('applications', identifier,
                           {'id': identifier, 'job_id': identifier, 'stage': 'saved'})
        result = snapshot(self.root)
        self.assertEqual(len(result['jobs']), 4, 'Tracked roles must remain available to applications')
        self.assertEqual(result['history'], [
            {'date': '2026-11-13', 'companies': {'Example': 2}, 'total': 2}])

    def test_snapshot_services_use_observed_context_without_claiming_api_connection(self):
        self.store.put('research', 'leetcode-context', {
            'id': 'leetcode-context', 'kind': 'service_context', 'service': 'leetcode',
            'account_handle': 'example-account', 'browser_access': 'signed_in',
            'observed_at': '2026-11-14', 'text': 'Observed profile with 0 solved problems.',
            'job_id': 'job-1', 'company': 'Example', 'password': 'not-for-the-browser',
            'unexpected_metadata': 'private-import-only',
            'profile_url': 'https://leetcode.com/u/example-account/',
            'sources': [{'url': 'https://leetcode.com/u/example-account/', 'title': 'Profile'}]})
        result = snapshot(self.root)
        self.assertEqual(len(result['services']), 4)
        leetcode = next(s for s in result['services'] if s['id'] == 'leetcode')
        self.assertEqual(leetcode['account_handle'], 'example-account')
        self.assertEqual(leetcode['access_status'], 'signed_in')
        self.assertEqual(leetcode['connection_status'], 'not_connected')
        self.assertRegex(result['app_build'], r'^[a-f0-9]{16}$')
        self.assertEqual(result['research'], [])
        self.assertNotIn('not-for-the-browser', json.dumps(result))
        detail = job_detail(self.root, 'job-1')
        self.assertEqual(detail['research'], [])
        self.assertEqual(detail['preparation_guide']['research'], [])
        self.assertNotIn('private-import-only', json.dumps(detail))

    def test_role_guide_is_available_without_application_or_interview_and_is_read_only(self):
        before = self.store.db.execute('SELECT * FROM records ORDER BY kind,id').fetchall()
        result = job_detail(self.root, 'job-1')
        guide = result['preparation_guide']
        self.assertEqual(guide['job_id'], 'job-1')
        self.assertEqual(guide['company'], 'Example')
        self.assertTrue(guide['steps'])
        self.assertIsNone(result['application'])
        self.assertEqual(result['interviews'], [])
        self.assertEqual(self.store.db.execute('SELECT * FROM records ORDER BY kind,id').fetchall(), before)

    def test_role_guide_research_keeps_company_context_without_other_role_claims(self):
        for row in [
            {'id': 'company', 'company': 'Example', 'text': 'Company process', 'sources': []},
            {'id': 'role', 'company': 'Example', 'job_id': 'job-1', 'text': 'Role format', 'sources': []},
            {'id': 'other-role', 'company': 'Example', 'job_id': 'job-2', 'text': 'Another team', 'sources': []},
            {'id': 'other-company', 'company': 'Elsewhere', 'text': 'Unrelated', 'sources': []},
        ]:
            self.store.put('research', row['id'], row)
        result = job_detail(self.root, 'job-1')
        self.assertEqual({r['id'] for r in result['preparation_guide']['research']}, {'company', 'role'})

    def test_rebuild_without_interviews_keeps_a_shared_schedule_and_role_guide(self):
        perform_action(self.root, {'action': 'prep'})
        result = snapshot(self.root)
        tasks = result['tasks']
        self.assertTrue(tasks)
        self.assertEqual(result['interviews'], [])
        self.assertTrue(all(not task.get('interview_id') for task in tasks))
        self.assertTrue(job_detail(self.root, 'job-1')['preparation_guide']['steps'])
        totals = {}
        for task in tasks:
            if task['status'] not in ('skipped', 'unscheduled'):
                totals[task['due_date']] = totals.get(task['due_date'], 0) + task['minutes']
        self.assertTrue(all(minutes <= result['settings']['preparation']['daily_minutes'] for minutes in totals.values()))

    def test_agent_preview_and_apply_use_cli_and_reject_stale_plan(self):
        before = self.store.db.execute('SELECT * FROM records ORDER BY kind,id').fetchall()
        preview = perform_action(self.root, {'action': 'agent', 'operation': 'preview', 'job_id': 'job-1'})
        self.assertTrue(preview['ok'])
        self.assertEqual(self.store.db.execute('SELECT * FROM records ORDER BY kind,id').fetchall(), before)
        self.assertTrue(preview['proposal']['next_steps'])
        apply = {'action': 'agent', 'operation': 'apply', 'job_id': 'job-1',
                 'fingerprint': preview['proposal']['fingerprint']}
        self.assertTrue(perform_action(self.root, apply)['applied'])
        self.assertTrue(self.store.all('tasks'))
        with self.assertRaises(WebError):
            perform_action(self.root, apply)
        self.assertEqual(list((self.root / 'inputs/web').iterdir()), [])

    def test_cli_application_roundtrip_preserves_notes_metadata_and_submission_time(self):
        self.store.put('applications', 'job-1', {'id': 'job-1', 'job_id': 'job-1', 'stage': 'saved',
                      'notes': 'Keep this', 'referral_events': [{'source': 'User record'}]})
        result = perform_action(self.root, {'action': 'application', 'job_ids': ['job-1'], 'stage': 'applied'})
        self.assertTrue(result['ok'])
        row = self.store.get('applications', 'job-1')
        self.assertEqual(row['notes'], 'Keep this')
        self.assertEqual(row['referral_events'], [{'source': 'User record'}])
        self.assertTrue(row['applied_at'])
        original_time = row['applied_at']
        perform_action(self.root, {'action': 'application', 'job_id': 'job-1', 'stage': 'recruiter',
                                   'follow_up_date': '2026-12-01', 'next_action': 'Reply to recruiter'})
        row = self.store.get('applications', 'job-1')
        self.assertEqual(row['applied_at'], original_time)
        self.assertEqual(row['follow_up_date'], '2026-12-01')
        self.assertEqual(list((self.root / 'inputs/web').iterdir()), [])

    def test_metadata_edits_do_not_date_undated_historical_submission(self):
        self.store.put('applications', 'job-1', {'id': 'job-1', 'job_id': 'job-1', 'stage': 'applied'})
        perform_action(self.root, {'action': 'application', 'job_id': 'job-1', 'stage': 'applied', 'notes': 'Follow up'})
        self.assertNotIn('applied_at', self.store.get('applications', 'job-1'))

    def test_save_queue_does_not_regress_existing_interview(self):
        self.store.put('applications', 'job-1', {'id': 'job-1', 'job_id': 'job-1', 'stage': 'technical'})
        with patch('jobflow.web._run_cli') as cli:
            result = perform_action(self.root, {'action': 'application', 'job_id': 'job-1', 'stage': 'saved'})
        cli.assert_not_called()
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(self.store.get('applications', 'job-1')['stage'], 'technical')

    def test_invalid_batch_is_rejected_before_any_write(self):
        with patch('jobflow.web._run_cli') as cli:
            with self.assertRaises(WebError):
                perform_action(self.root, {'action': 'application', 'job_ids': ['job-1', 'unknown'], 'stage': 'applied'})
            with self.assertRaises(WebError):
                perform_action(self.root, {'action': 'application', 'job_id': 'job-1', 'stage': 'applied',
                                           'follow_up_date': '2026-02-30'})
        cli.assert_not_called()
        self.assertEqual(self.store.all('applications'), [])

    def test_completed_task_edit_preserves_completion_and_unexposed_metadata(self):
        row = {'id': 'task-1', 'status': 'done', 'title': 'Practice', 'minutes': 30,
               'due_date': '2026-11-13', 'completed_at': '2026-11-13T14:00:00-08:00',
               'notes': 'Original', 'custom_history': ['kept'], 'actual_minutes': 25}
        self.store.put('tasks', row['id'], row)
        perform_action(self.root, {'action': 'task', 'id': row['id'], 'notes': 'Edited notes'})
        updated = self.store.get('tasks', row['id'])
        self.assertEqual(updated['completed_at'], row['completed_at'])
        self.assertEqual(updated['custom_history'], ['kept'])
        self.assertEqual(updated['actual_minutes'], 25)

    def test_lock_contention_exit_zero_is_reported_as_conflict(self):
        with patch('jobflow.web.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='',
                   stderr='Another workflow process is running; try again later.')):
            with self.assertRaises(WebError) as raised:
                _run_cli(self.root, ['prep'])
        self.assertEqual(raised.exception.status, 409)

    def test_interview_requires_explicit_timezone(self):
        with patch('jobflow.web._run_cli') as cli, self.assertRaises(ValueError):
            perform_action(self.root, {'action': 'interview', 'job_id': 'job-1',
                                      'starts_at': '2026-12-01T10:00:00', 'round': 'Coding'})
        cli.assert_not_called()


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        (self.assets / 'index.html').write_text('<h1>Local Jobflow</h1>')
        (self.assets / 'private.json').write_text('{"secret":"not-served"}')
        self.server = JobflowServer(('127.0.0.1', 0), self.root, self.assets)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        result = response.status, response.read(), response.getheaders()
        connection.close()
        return result

    def test_host_origin_token_and_static_file_boundary(self):
        status, body, headers = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        token = json.loads(body)['csrf_token']
        self.assertEqual(status, 200)
        for path in ('/private.json', '/../config/profile.json', '/%2e%2e/config/settings.json'):
            self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.request('GET', '/', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/state', headers={'Origin': 'https://evil.example'})[0], 403)
        payload = json.dumps({'action': 'prep'})
        self.assertEqual(self.request('POST', '/api/action', payload, {'Content-Type': 'application/json'})[0], 403)
        with patch('jobflow.web.perform_action', return_value={'ok': True}) as action:
            self.assertEqual(self.request('POST', '/api/action', payload,
                             {'Content-Type': 'application/json', 'X-CSRF-Token': token})[0], 200)
        action.assert_called_once()
        self.assertEqual(dict(headers)['Cache-Control'], 'no-store')

    def test_server_cannot_bind_to_all_interfaces(self):
        with self.assertRaises(ValueError):
            JobflowServer(('0.0.0.0', 0), self.root, self.assets)

    def test_profile_api_uses_local_origin_boundary_and_does_not_initialize_workspace(self):
        status, body, headers = self.request('GET', '/api/profile')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {
            'profile': {'name': '', 'headline': '', 'skills': [], 'evidence': []}})
        self.assertEqual(dict(headers)['Cache-Control'], 'no-store')
        self.assertFalse((self.root / 'data').exists())
        self.assertFalse((self.root / 'config').exists())
        self.assertEqual(self.request('GET', '/api/profile', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/profile', headers={'Origin': 'https://evil.example'})[0], 403)

    def test_only_named_font_and_art_assets_are_served(self):
        public = {
            'fonts/dm-serif-display-latin.woff2': (b'wOF2display', 'font/woff2'),
            'fonts/nunito-latin.woff2': (b'wOF2nunito', 'font/woff2'),
            'art/jobflow-cosmos.webp': (b'RIFFtestWEBP', 'image/webp'),
        }
        for name, (payload, mime) in public.items():
            target = self.assets / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(payload)
            with self.subTest(name=name):
                status, body, headers = self.request('GET', '/' + name)
                self.assertEqual(status, 200)
                self.assertEqual(body, payload)
                self.assertEqual(dict(headers)['Content-Type'], mime)
                self.assertEqual(dict(headers)['X-Content-Type-Options'], 'nosniff')
        for name in ('fonts/private.woff2', 'art/private.webp'):
            (self.assets / name).write_bytes(b'not-public')
        for path in ('/fonts/private.woff2', '/art/private.webp',
                     '/fonts/../private.json', '/art/%2e%2e/fonts/nunito-latin.woff2'):
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path)[0], 404)

    def test_allowed_asset_symlink_cannot_leave_public_directory(self):
        (self.assets / 'fonts').mkdir()
        private = self.root / 'private.woff2'
        private.write_bytes(b'not-public')
        (self.assets / 'fonts/nunito-latin.woff2').symlink_to(private)
        self.assertEqual(self.request('GET', '/fonts/nunito-latin.woff2')[0], 404)

    def test_dashboard_assets_are_named_and_map_policy_is_limited_to_map_origins(self):
        public = {
            'my-account.js': b'window.JobflowAccount = {};',
            'my-account.css': b'.my-account {}',
            'trends.js': b'window.JobflowTrends = {};',
            'resume-controls.js': b'window.JobflowResumeControls = {};',
            'resume-controls.css': b'.resume-picker {}',
            'trends.css': b'.jf-trend {}',
            'maps.js': b'window.JobflowMaps = {};',
            'maps.css': b'.jf-map {}',
            'vendor/leaflet/leaflet.js': b'window.L = {};',
            'vendor/leaflet/leaflet.css': b'.leaflet-container {}',
            'vendor/maplibre/maplibre-gl-csp.js': b'window.maplibregl = {};',
            'vendor/maplibre/maplibre-gl-csp-worker.js': b'self.onmessage = function() {};',
            'vendor/maplibre/maplibre-gl.css': b'.maplibregl-map {}',
            'vendor/maplibre/leaflet-maplibre-gl.js': b'window.L.maplibreGL = function() {};',
        }
        for name, payload in public.items():
            target = self.assets / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            with self.subTest(name=name):
                status, body, headers = self.request('GET', '/' + name + '?v=build')
                self.assertEqual(status, 200)
                self.assertEqual(body, payload)
                self.assertEqual(dict(headers)['X-Content-Type-Options'], 'nosniff')
                if name.endswith('.js'):
                    self.assertIn(dict(headers)['Content-Type'],
                                  ('text/javascript; charset=utf-8', 'application/javascript; charset=utf-8'))
        for name in ('vendor/leaflet/private.json', 'vendor/leaflet/leaflet.js.map',
                     'vendor/maplibre/private.json', 'vendor/maplibre/maplibre-gl-csp.js.map',
                     'vendor/maplibre/unlisted-worker.js'):
            (self.assets / name).write_bytes(b'not-public')
            self.assertEqual(self.request('GET', '/' + name)[0], 404)
        unused_status, unused_body, headers = self.request('GET', '/')
        headers = dict(headers)
        policy = {part.strip().split(' ', 1)[0]: part.strip().split(' ', 1)[1]
                  for part in headers['Content-Security-Policy'].split(';') if part.strip()}
        self.assertEqual(policy['img-src'].split(), ["'self'", 'data:', 'blob:',
                         'https://tile.openstreetmap.org', 'https://tiles.openfreemap.org'])
        self.assertEqual(policy['script-src'], "'self'")
        self.assertEqual(policy['worker-src'], "'self'")
        self.assertEqual(policy['connect-src'].split(), ["'self'", 'https://tiles.openfreemap.org'])
        self.assertEqual(policy['default-src'], "'self'")
        self.assertEqual(policy['font-src'], "'self' blob:")
        self.assertEqual(policy['base-uri'], "'none'")
        self.assertEqual(policy['frame-ancestors'], "'none'")
        self.assertEqual(policy['form-action'], "'self'")
        self.assertEqual(headers['Referrer-Policy'], 'strict-origin-when-cross-origin')

    def test_build_version_tracks_chart_map_and_vendored_assets(self):
        previous = json.loads(self.request('GET', '/api/version')[1])['app_build']
        for name in ('trends.js', 'trends.css', 'maps.js', 'maps.css',
                     'vendor/leaflet/leaflet.js', 'vendor/leaflet/leaflet.css',
                     'vendor/maplibre/maplibre-gl-csp.js',
                     'vendor/maplibre/maplibre-gl-csp-worker.js',
                     'vendor/maplibre/maplibre-gl.css', 'vendor/maplibre/leaflet-maplibre-gl.js'):
            target = self.assets / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'new public presentation asset')
            current = json.loads(self.request('GET', '/api/version')[1])['app_build']
            with self.subTest(name=name):
                self.assertNotEqual(current, previous)
            previous = current
        (self.assets / 'private.json').write_bytes(b'private data must not affect the UI build')
        self.assertEqual(json.loads(self.request('GET', '/api/version')[1])['app_build'], previous)

    def test_build_version_tracks_assets_and_is_injected_into_entry_page(self):
        (self.assets / 'index.html').write_text(
            '<meta name="jobflow-build" content="__JOBFLOW_BUILD__">'
            '<script src="/app.js?v=__JOBFLOW_BUILD__"></script>')
        (self.assets / 'app.js').write_text('console.log("first");')
        status, body, headers = self.request('GET', '/api/version')
        self.assertEqual(status, 200)
        first = json.loads(body)['app_build']
        self.assertRegex(first, r'^[a-f0-9]{16}$')
        self.assertEqual(dict(headers)['Cache-Control'], 'no-store')
        status, body, unused_headers = self.request('GET', '/index.html')
        self.assertNotIn(b'__JOBFLOW_BUILD__', body)
        self.assertIn(('content="' + first + '"').encode(), body)
        self.assertIn(('app.js?v=' + first).encode(), body)
        self.assertEqual(self.request('GET', '/app.js?v=' + first)[0], 200)
        state = json.loads(self.request('GET', '/api/state')[1])
        self.assertEqual(state['app_build'], first)
        (self.assets / 'app.js').write_text('console.log("second");')
        self.assertNotEqual(json.loads(self.request('GET', '/api/version')[1])['app_build'], first)
        self.assertFalse((self.root / 'data').exists(), 'Version checks must remain read-only')

    def test_large_json_compresses_only_when_client_accepts_gzip(self):
        with patch('jobflow.web.snapshot', return_value={'jobs': [{'title': 'Example role'}] * 200}):
            status, body, headers = self.request('GET', '/api/state', headers={'Accept-Encoding': 'gzip'})
            self.assertEqual(status, 200)
            self.assertEqual(dict(headers)['Content-Encoding'], 'gzip')
            self.assertEqual(len(json.loads(gzip.decompress(body))['jobs']), 200)
            status, body, headers = self.request('GET', '/api/state', headers={'Accept-Encoding': 'gzip;q=0'})
            self.assertNotIn('Content-Encoding', dict(headers))
            self.assertEqual(len(json.loads(body)['jobs']), 200)


if __name__ == '__main__':
    unittest.main()
