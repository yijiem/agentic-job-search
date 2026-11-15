import copy
import json
import tempfile
import unittest
from pathlib import Path

from jobflow.locations import (location_catalog, location_reviews, office_locations,
                              validate_company_location, validate_location_review)
from jobflow.preparation_guides import build_role_guide
from jobflow.reports import write_reports
from jobflow.storage import Store
from jobflow.tracking import validate_record
from jobflow.web import job_detail, snapshot


def office(**updates):
    row = {'id': 'example-sf-office', 'kind': 'company_location', 'company': 'Example',
           'city': 'San Francisco', 'address': '100 Market Street, San Francisco, CA 94105',
           'relationship': 'company_office', 'precision': 'address',
           'source_url': 'https://example.com/contact', 'observed_at': '2026-11-14',
           'text': 'The employer lists this office address. Individual role worksite is unconfirmed.',
           'sources': [{'url': 'https://example.com/contact', 'title': 'Employer office page'}]}
    row.update(updates)
    return row


def with_coordinates(**updates):
    row = office(latitude=37.793, longitude=-122.394,
                 coordinate_source_url='https://example.com/map',
                 coordinate_method='employer_map_link', precision='building',
                 sources=[{'url': 'https://example.com/contact', 'title': 'Employer office page'},
                          {'url': 'https://example.com/map', 'title': 'Employer map link'}])
    row.update(updates)
    return row


def review(**updates):
    row = {'id': 'location-review-example', 'kind': 'company_location_review', 'company': 'Example',
           'status': 'verified', 'location_ids': ['example-sf-office'], 'observed_at': '2026-11-14',
           'text': 'Reviewed the public employer address evidence.',
           'sources': [{'url': 'https://example.com/contact', 'title': 'Official employer contact page'}]}
    row.update(updates)
    return row


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/settings.json').write_text(json.dumps({
            'search': {'min_score': 40}, 'schedule': {'timezone': 'America/Los_Angeles'}}))
        self.store = Store(self.root / 'data/jobflow.sqlite3')
        self.job = {'id': 'job-1', 'company': 'Example', 'title': 'Staff Engineer',
                    'location': 'San Francisco, CA; Mountain View, CA', 'remote': False,
                    'eligible': True, 'score': 90, 'availability': 'open',
                    'url': 'https://example.com/jobs/1', 'description': 'Build systems.',
                    'first_seen': '2026-11-01T12:00:00Z'}
        self.store.put('jobs', 'job-1', self.job)
        self.store.put('jobs', 'other-company', dict(self.job, id='other-company', company='Other'))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_office_import_preserves_metadata_and_accepts_ungeocoded_address(self):
        original = office(private_metadata={'review': 'retain locally'})
        before = copy.deepcopy(original)
        row = validate_record('research', original, self.store)
        self.assertEqual(original, before)
        self.assertEqual(row['private_metadata'], original['private_metadata'])
        self.assertNotIn('latitude', row)
        self.assertEqual(row['job_ids'], [])
        self.store.put('research', row['id'], row)
        updated = validate_record('research', {'id': row['id'], 'text': 'Reviewed again.'}, self.store)
        self.assertEqual(updated['address'], row['address'])
        self.assertEqual(updated['private_metadata'], original['private_metadata'])
        self.assertEqual(self.store.get('jobs', 'job-1'), self.job)

    def test_coordinate_validation_rejects_partial_nonfinite_and_out_of_range(self):
        for changes in ({'latitude': None}, {'longitude': None}, {'latitude': float('nan')},
                        {'longitude': float('inf')}, {'latitude': 90.01}, {'latitude': -90.01},
                        {'longitude': 180.01}, {'longitude': -180.01}, {'latitude': True},
                        {'longitude': '-122.4'}, {'latitude': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_record('research', with_coordinates(**changes), self.store)
        self.assertEqual(validate_record('research', with_coordinates(latitude=90, longitude=-180), self.store)['latitude'], 90.0)

    def test_required_fields_dates_relationships_and_precision_are_validated(self):
        for key in ('id', 'company', 'city', 'address', 'text', 'source_url', 'observed_at', 'sources', 'relationship', 'precision'):
            value = office()
            value.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                validate_record('research', value, self.store)
        for changes in ({'observed_at': '2026-02-30'}, {'observed_at': '2026-11-14T12:00:00Z'},
                        {'observed_at': '2026-1-4'}, {'observed_at': 20261114},
                        {'relationship': 'headquarters_guess'}, {'precision': 'city'},
                        {'company': ['Example']}, {'address': '  '}, {'job_ids': 'job-1'},
                        {'job_ids': [None]}, {'job_ids': ['../../private']}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_record('research', office(**changes), self.store)

    def test_sources_must_be_safe_and_coordinate_evidence_must_be_listed(self):
        for url in ('javascript:alert(1)', 'file:///private/data', 'https:missing-host',
                    'https://name:secret@example.com', 'https://example.com/a b',
                    'https://example.com:invalid/map'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_record('research', office(source_url=url, sources=[{'url': url}]), self.store)
        for changes in ({'sources': ['https://example.com/contact']},
                        {'sources': [{'url': 'https://unrelated.example/page'}]},
                        {'sources': [{'url': 'https://example.com/contact', 'title': []}]},
                        {'coordinate_source_url': 'https://example.com/unlisted-map'},
                        {'coordinate_source_url': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_record('research', with_coordinates(**changes), self.store)

    def test_interpolated_coordinates_remain_address_precision_with_note(self):
        row = with_coordinates(precision='address', coordinate_method='census_address_range',
                               coordinate_note='Address-range interpolation; approximate, not an entrance.')
        valid = validate_record('research', row, self.store)
        public = location_catalog([valid])[0]
        self.assertEqual(public['coordinate_method'], 'census_address_range')
        self.assertIn('approximate', public['coordinate_note'])
        for changes in ({'precision': 'building'}, {'coordinate_note': ''},
                        {'coordinate_method': 'unknown_geocoder'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_record('research', dict(row, **changes), self.store)

    def test_osm_address_points_require_precision_and_approximation_note(self):
        for method in ('openstreetmap_address_point', 'published_address_point'):
            row = with_coordinates(precision='address', coordinate_method=method,
                                   coordinate_note='Published address point; approximate building centroid, not an entrance.')
            self.assertEqual(validate_record('research', row, self.store)['precision'], 'address')
            for changes in ({'precision': 'building'}, {'coordinate_note': ''},
                            {'latitude': None, 'longitude': None}):
                with self.subTest(method=method, changes=changes), self.assertRaises(ValueError):
                    validate_record('research', dict(row, **changes), self.store)

    def test_review_import_validates_status_sources_and_current_company_references(self):
        self.store.put('research', 'example-sf-office', office())
        original = review(private_metadata='preserved locally')
        row = validate_record('research', original, self.store)
        self.assertEqual(row['private_metadata'], original['private_metadata'])
        self.assertEqual(validate_record('research', review(status='remote_only', location_ids=[]), self.store)['status'], 'remote_only')
        for changes in ({'id': ''}, {'id': '../bad'}, {'company': ''}, {'status': 'address_only'},
                        {'status': 'not_found'}, {'observed_at': '2026-02-30'}, {'text': ''},
                        {'sources': []}, {'sources': [{'url': 'javascript:alert(1)'}]},
                        {'sources': [{'url': 'https://u:p@example.com/'}]},
                        {'sources': [{'url': 'https://example.com', 'title': []}]},
                        {'location_ids': []}, {'location_ids': ['missing']},
                        {'location_ids': ['example-sf-office', 'example-sf-office']},
                        {'location_ids': '../bad'}, {'company': 'Other'}, {'status': 'remote_only'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_record('research', review(**changes), self.store)
        for field in ('id', 'company', 'status', 'observed_at', 'sources', 'text'):
            incomplete = review()
            incomplete.pop(field)
            with self.subTest(missing=field), self.assertRaises(ValueError):
                validate_location_review(incomplete)

    def test_review_projection_covers_every_company_and_derives_current_coordinate_status(self):
        offices = location_catalog([with_coordinates(), office(id='address', company='Address')])
        rows = [review(private_metadata='never-public'),
                review(id='old-remote', company='Remote', status='unverified', location_ids=[], observed_at='2026-11-13'),
                review(id='remote', company='Remote', status='remote_only', location_ids=[]),
                review(id='stale', company='Stale', location_ids=['no-longer-valid']),
                review(id='bad-url', company='Unsafe', status='remote_only', location_ids=[], sources=[{'url': 'file:///secret'}])]
        rows[0]['sources'][0]['token'] = 'never-public'
        before = copy.deepcopy(rows)
        companies = ['Example', 'Address', 'Remote', 'Stale', 'Unsafe', 'Unreviewed']
        result = location_reviews(rows, offices, companies)
        by_company = {row['company']: row for row in result}
        self.assertEqual(set(by_company), set(companies))
        self.assertEqual({name: row['status'] for name, row in by_company.items()},
                         {'Example': 'verified', 'Address': 'address_only', 'Remote': 'remote_only',
                          'Stale': 'unverified', 'Unsafe': 'unverified', 'Unreviewed': 'unverified'})
        self.assertEqual(by_company['Stale']['location_ids'], [])
        self.assertEqual(by_company['Stale']['review_status'], 'verified')
        self.assertFalse(by_company['Unsafe']['reviewed'])
        self.assertTrue(by_company['Address']['reviewed'])
        self.assertNotIn('never-public', json.dumps(result))
        self.assertEqual(rows, before)
        # A research status cannot create coordinates or suppress saved office evidence.
        changed = location_reviews([review(status='remote_only', location_ids=[])], offices, ['Example'])
        self.assertEqual(changed[0]['status'], 'verified')
        removed = location_reviews(rows, location_catalog([office()]), ['Example'])
        self.assertEqual(removed[0]['status'], 'address_only')

    def test_location_reviews_are_projected_separately_from_general_research_and_prep(self):
        row = review(status='remote_only', location_ids=[], private_note='never-in-browser',
                     text='Company-wide remote evidence, unrelated to interview preparation.')
        self.store.put('research', row['id'], row)
        self.store.put('jobs', 'closed', dict(self.job, id='closed', company='Closed', availability='closed'))
        self.store.put('jobs', 'not-matching', dict(self.job, id='not-matching', company='Low Score', score=1))
        before = [tuple(row) for row in self.store.db.execute('SELECT * FROM records ORDER BY kind,id')]
        current = snapshot(self.root)
        projected = {row['company']: row for row in current['location_reviews']}
        self.assertEqual(set(projected), {'Example', 'Other'})
        self.assertEqual(projected['Example']['status'], 'remote_only')
        self.assertEqual(current['research'], [])
        self.assertNotIn('never-in-browser', json.dumps(current))
        detail = job_detail(self.root, 'job-1')
        self.assertEqual(detail['research'], [])
        self.assertNotIn(row['text'], json.dumps(detail['preparation_guide']))
        self.assertEqual(build_role_guide(self.job, {}, [row, office()])['research'], [])
        self.assertEqual(before, [tuple(row) for row in self.store.db.execute('SELECT * FROM records ORDER BY kind,id')])

    def test_stale_job_site_cannot_keep_company_coverage_verified(self):
        evidence = with_coordinates(relationship='job_site', job_ids=['job-1'],
                                    sources=[{'url': 'https://example.com/contact'},
                                             {'url': 'https://example.com/map'}, {'url': self.job['url']}])
        self.store.put('research', evidence['id'], evidence)
        self.store.put('research', 'location-review-example', review())
        # Keep the company in the matching directory when the linked role stops
        # matching. This other role has never been confirmed at the address.
        self.store.put('jobs', 'same-company', dict(self.job, id='same-company'))
        catalog = location_catalog([evidence])
        self.assertEqual(location_reviews([review()], catalog, ['Example'], jobs=[self.job])[0]['status'], 'verified')
        self.assertEqual(location_reviews([review()], catalog, ['Example'])[0]['status'], 'unverified')
        settings = {'search': {'min_score': 40}}
        for changes in ({}, {'location': 'Menlo Park, CA'}, {'location': 'Remote - United States'},
                        {'url': 'https://example.com/jobs/replaced'}, {'company': 'Other'},
                        {'availability': 'closed'}, {'eligible': False}, {'score': 39}):
            with self.subTest(changes=changes):
                self.store.put('jobs', self.job['id'], dict(self.job, **changes))
                before = [tuple(row) for row in self.store.db.execute('SELECT * FROM records ORDER BY kind,id')]
                current = snapshot(self.root)
                write_reports(self.root, self.store, settings)
                coverage = json.loads((self.root / 'reports/location_enrichment.json').read_text())
                projected = next(row for row in current['location_reviews'] if row['company'] == 'Example')
                exported = next(row for row in coverage['company_reviews'] if row['company'] == 'Example')
                expected = 'unverified' if changes else 'verified'
                self.assertEqual(projected['status'], expected)
                self.assertEqual(exported, projected)
                self.assertEqual(projected['review_status'], 'verified')
                self.assertEqual(projected['locations_count'], 0 if changes else 1)
                self.assertEqual(projected['coordinates_count'], 0 if changes else 1)
                self.assertEqual(projected['location_ids'], [] if changes else [evidence['id']])
                self.assertEqual(coverage['with_confirmed_jobsite'], 0 if changes else 1)
                # The historical source record stays available and unmodified.
                self.assertEqual(current['locations'], catalog)
                self.assertEqual(json.loads((self.root / 'reports/locations.json').read_text()), catalog)
                self.assertEqual(before, [tuple(row) for row in self.store.db.execute('SELECT * FROM records ORDER BY kind,id')])
        # Independent company-office evidence remains valid without any linked
        # current job and is not suppressed by the stale posting record.
        retained = location_catalog([evidence, with_coordinates(id='independent-office')])
        self.assertEqual(location_reviews([review()], retained, ['Example'])[0]['location_ids'], ['independent-office'])

    def test_confirmed_job_sites_require_explicit_known_same_company_jobs(self):
        for job_ids in ([], ['missing'], ['other-company'], ['job-1', 'job-1']):
            with self.subTest(job_ids=job_ids), self.assertRaises(ValueError):
                validate_record('research', office(relationship='job_site', job_ids=job_ids), self.store)
        with self.assertRaises(ValueError):
            validate_record('research', office(job_ids=['job-1']), self.store)
        with self.assertRaisesRegex(ValueError, 'posting URL'):
            validate_record('research', office(relationship='job_site', job_ids=['job-1']), self.store)
        row = validate_record('research', office(relationship='job_site', job_ids=['job-1'],
                              sources=[{'url': 'https://example.com/contact'},
                                       {'url': self.job['url'], 'title': 'Posting with street address'}]), self.store)
        linked = office_locations(self.job, location_catalog([row]))
        self.assertTrue(linked[0]['worksite_confirmed'])
        self.assertEqual(linked[0]['label'], 'Posting-confirmed worksite')
        self.assertEqual(office_locations(dict(self.job, id='unlinked'), location_catalog([row])), [])
        self.assertEqual(office_locations(dict(self.job, company='Other'), location_catalog([row])), [])

    def test_confirmed_site_does_not_follow_changed_posting_city_or_source(self):
        evidence = office(relationship='job_site', job_ids=['job-1'],
                          sources=[{'url': 'https://example.com/contact'}, {'url': self.job['url']}])
        row = validate_record('research', evidence, self.store)
        catalog = location_catalog([row])
        self.store.put('research', row['id'], row)
        for changes in ({'location': 'Menlo Park, CA'}, {'location': 'Remote - United States'},
                        {'location': 'San Francisco Bay Area'}, {'url': 'https://example.com/jobs/2'}):
            with self.subTest(changes=changes):
                changed = dict(self.job, **changes)
                self.assertEqual(office_locations(changed, catalog), [])
                self.store.put('jobs', changed['id'], changed)
                detail = job_detail(self.root, changed['id'])
                self.assertEqual(detail['locations'], [])
                current = snapshot(self.root)
                self.assertEqual(next(job for job in current['jobs'] if job['id'] == changed['id'])['office_locations'], [])
                self.assertEqual(current['locations'][0]['observed_at'], '2026-11-14')
                with self.assertRaises(ValueError):
                    validate_record('research', evidence, self.store)
        # An explicitly sourced posting may permit telecommuting while still
        # naming the city; do not turn that into an unsourced office inference.
        linked = office_locations(dict(self.job, remote=True), catalog)
        self.assertTrue(linked[0]['worksite_confirmed'])
        self.assertEqual(linked[0]['observed_at'], row['observed_at'])

    def test_office_references_require_city_compatibility_and_nonremote_posting(self):
        catalog = location_catalog([office()])
        linked = office_locations(self.job, catalog)
        self.assertEqual(len(linked), 1)
        self.assertFalse(linked[0]['worksite_confirmed'])
        self.assertIn('unconfirmed', linked[0]['label'])
        for location in ('South San Francisco, CA', 'San Francisco Bay Area',
                         'Greater San Francisco', 'San Francisco County', 'San Francisco metro area',
                         'Bay Area', 'Mountain View, CA', 'Remote-Friendly | San Francisco, CA', ''):
            with self.subTest(location=location):
                self.assertEqual(office_locations(dict(self.job, location=location), catalog), [])
        self.assertEqual(office_locations(dict(self.job, remote=True), catalog), [])
        self.assertEqual(office_locations(dict(self.job, company='Other'), catalog), [])
        self.assertEqual(office_locations(dict(self.job, location='East Palo Alto'),
                                         location_catalog([office(city='Palo Alto')])), [])

    def test_catalog_ignores_invalid_rows_and_exposes_only_allowlisted_fields(self):
        row = with_coordinates(private_metadata='hidden', password='never exposed',
                               worksite_confirmed=True)
        row['sources'][0]['private_key'] = 'never exposed'
        before = copy.deepcopy(row)
        catalog = location_catalog([row, office(id='invalid', latitude=float('nan'), longitude=0),
                                    {'kind': 'ordinary_research'}, None])
        self.assertEqual(len(catalog), 1)
        self.assertFalse(catalog[0]['worksite_confirmed'])
        self.assertNotIn('never exposed', json.dumps(catalog))
        self.assertNotIn('private_metadata', catalog[0])
        self.assertEqual(row, before)

    def test_snapshot_and_detail_derive_locations_without_mutating_job_or_history(self):
        before = snapshot(self.root)
        row = validate_record('research', with_coordinates(private_metadata='not-for-browser'), self.store)
        self.store.put('research', row['id'], row)
        records_before = self.store.db.execute('SELECT * FROM records ORDER BY kind,id').fetchall()
        current = snapshot(self.root)
        detail = job_detail(self.root, 'job-1')
        job = next(job for job in current['jobs'] if job['id'] == 'job-1')
        self.assertEqual(job['location'], self.job['location'])
        self.assertEqual(len(job['office_locations']), 1)
        self.assertEqual(current['locations'], job['office_locations'])
        self.assertEqual(detail['job']['office_locations'], job['office_locations'])
        self.assertEqual(detail['locations'], job['office_locations'])
        self.assertFalse(job['office_locations'][0]['worksite_confirmed'])
        self.assertEqual(current['history'], before['history'])
        self.assertEqual(current['totals'], before['totals'])
        self.assertEqual(current['research'], [])
        self.assertEqual(detail['research'], [])
        self.assertNotIn('not-for-browser', json.dumps(current))
        self.assertNotIn('not-for-browser', json.dumps(detail))
        self.assertEqual(self.store.get('jobs', 'job-1'), self.job)
        self.assertEqual(self.store.db.execute('SELECT * FROM records ORDER BY kind,id').fetchall(), records_before)


if __name__ == '__main__':
    unittest.main()
