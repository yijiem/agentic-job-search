import copy
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from jobflow import workflow
from jobflow.storage import Store


class SearchPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'data/jobflow.sqlite3')
        self.config = {
            'search': {'min_score': 40, 'target_tc': 500000},
            'sources': [{'id': name, 'company': name, 'kind': 'greenhouse',
                         'url': 'https://example.com/' + name, 'board': name}
                        for name in ('alpha', 'beta', 'gamma')],
        }
        self.profile = {'skills': ['Python'], 'evidence': [{'text': 'Built Python services.'}]}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def raw(self, source='alpha', number=1):
        return {'source_id': source, 'external_id': str(number), 'company': source,
                'title': 'Senior Software Engineer', 'location': 'San Francisco, CA',
                'description': 'Build backend services in Python.',
                'url': 'https://example.com/%s/jobs/%s' % (source, number),
                'compensation': {}}

    def search(self, snapshots, **kwargs):
        def fetch(source):
            value = snapshots.get(source['id'], [])
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        with patch.object(workflow, 'fetch_source', side_effect=fetch):
            return workflow.search(self.store, self.config, self.profile, **kwargs)

    def test_reads_job_corpus_once_and_scores_each_fetched_job_once(self):
        snapshots = {name: [self.raw(name, n) for n in range(3)]
                     for name in ('alpha', 'beta', 'gamma')}
        with patch.object(self.store, 'all', wraps=self.store.all) as reads, \
                patch.object(self.store, 'get', wraps=self.store.get) as gets, \
                patch.object(workflow, 'match_job', wraps=workflow.match_job) as matcher:
            result = self.search(snapshots)
        self.assertEqual(9, result['new_jobs'])
        self.assertEqual(9, result['matching_jobs'])
        self.assertEqual(9, matcher.call_count)
        self.assertEqual(['jobs'], [call.args[0] for call in reads.call_args_list])
        gets.assert_not_called()

    def test_unchanged_snapshot_refreshes_freshness_without_scoring_again(self):
        snapshots = {'alpha': [self.raw()]}
        with patch.object(workflow, 'utcnow', return_value='2026-11-14T16:00:00+00:00'):
            self.search(snapshots)
        original = self.store.all('jobs')[0]
        original['notes'] = 'Keep my research notes'
        self.store.put('jobs', original['id'], original)
        with patch.object(workflow, 'utcnow', return_value='2026-11-15T16:00:00+00:00'), \
                patch.object(workflow, 'match_job', wraps=workflow.match_job) as matcher:
            self.search(snapshots)
        matcher.assert_not_called()
        current = self.store.get('jobs', original['id'])
        self.assertEqual('2026-11-15T16:00:00+00:00', current['last_seen'])
        self.assertEqual(original['first_seen'], current['first_seen'])
        self.assertEqual(original['notes'], current['notes'])
        self.assertEqual(original['matching_input_signature'], current['matching_input_signature'])

    def test_changed_description_rescores_only_that_job(self):
        first, second = self.raw(number=1), self.raw(number=2)
        self.search({'alpha': [first, second]})
        changed = dict(first, description='Build storage systems in Rust.')
        with patch.object(workflow, 'match_job', wraps=workflow.match_job) as matcher:
            self.search({'alpha': [changed, second]})
        self.assertEqual(1, matcher.call_count)
        self.assertEqual(changed['url'], matcher.call_args.args[0]['url'])

    def test_profile_preferences_and_ranker_version_invalidate_cached_matching(self):
        snapshots = {'alpha': [self.raw()]}
        self.search(snapshots)
        for change in ('profile', 'preferences', 'ranker'):
            with self.subTest(change=change):
                if change == 'profile':
                    self.profile['skills'].append('Storage')
                elif change == 'preferences':
                    self.config['search']['min_score'] = 70
                version = workflow.MATCHING_VERSION + int(change == 'ranker')
                with patch.object(workflow, 'MATCHING_VERSION', version), \
                        patch.object(workflow, 'match_job', wraps=workflow.match_job) as matcher:
                    result = self.search(snapshots)
                self.assertEqual(1, matcher.call_count)
                if change == 'preferences':
                    self.assertEqual(0, result['matching_jobs'])

    def test_legacy_and_unfetched_jobs_rematch_once_without_rewriting_unchanged_rows(self):
        self.config['sources'] = []
        job, _ = workflow.ingest_job(self.store, self.raw('manual'), self.profile, self.config)
        del job['matching_input_signature']
        self.store.put('jobs', job['id'], job)
        with patch.object(workflow, 'match_job', wraps=workflow.match_job) as matcher:
            self.search({})
        self.assertEqual(1, matcher.call_count)
        with patch.object(workflow, 'match_job', wraps=workflow.match_job) as matcher, \
                patch.object(self.store, 'put_many', wraps=self.store.put_many) as writes:
            self.search({})
        matcher.assert_not_called()
        writes.assert_not_called()
        self.profile['skills'].append('Backend')
        with patch.object(workflow, 'match_job', wraps=workflow.match_job) as matcher:
            self.search({})
        self.assertEqual(1, matcher.call_count)

    def test_source_writes_use_bounded_batches_and_one_write_per_fetched_job(self):
        count = workflow.SEARCH_WRITE_BATCH_SIZE * 2 + 3
        with patch.object(self.store, 'put_many', wraps=self.store.put_many) as writes, \
                patch.object(self.store, 'put', wraps=self.store.put) as single_writes:
            self.search({'alpha': [self.raw(number=n) for n in range(count)]})
        batches = [call.args[1] for call in writes.call_args_list if call.args[0] == 'jobs']
        self.assertEqual(count, sum(len(batch) for batch in batches))
        self.assertTrue(all(len(batch) <= workflow.SEARCH_WRITE_BATCH_SIZE for batch in batches))
        self.assertEqual(3, len(batches))
        self.assertEqual(Counter({'source_health': 3}),
                         Counter(call.args[0] for call in single_writes.call_args_list))

    def test_raw_import_cannot_override_persisted_matching_using_cached_signature(self):
        first, _ = workflow.ingest_job(self.store, self.raw(), self.profile, self.config)
        raw = dict(self.raw(), score=999, eligible=False, reasons=['Untrusted'],
                   matching_input_signature=first['matching_input_signature'])
        with patch.object(workflow, 'match_job', wraps=workflow.match_job) as matcher:
            updated, fresh = workflow.ingest_job(self.store, raw, self.profile, self.config)
        matcher.assert_not_called()
        self.assertFalse(fresh)
        for key in workflow.MATCHING_RESULTS:
            self.assertEqual(first[key], updated[key])

    def test_cached_matching_preserves_success_failure_and_reappearance_lifecycle(self):
        self.search({'alpha': [self.raw()]})
        job = self.store.all('jobs')[0]
        with patch.object(workflow, 'match_job', wraps=workflow.match_job) as matcher:
            for snapshot, state in (([], ('missing', 1)),
                                    (RuntimeError('unavailable'), ('missing', 1)),
                                    ([], ('closed', 2)),
                                    ([self.raw()], ('open', 0))):
                self.search({'alpha': snapshot}, source_ids=['alpha'])
                current = self.store.get('jobs', job['id'])
                self.assertEqual(state, (current['availability'], current['missing_scans']))
        matcher.assert_not_called()


if __name__ == '__main__':
    unittest.main()
