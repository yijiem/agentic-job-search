import base64
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobflow.application_batches import perform_batch_action, resume_content
from jobflow.onboarding import initialize_workspace
from jobflow.resume_reviews import (REVIEW_KIND, perform_review_action, review_detail,
                                    review_list, resolve_text)
from jobflow.resumes import RESUME_KIND, resume_library
from jobflow.storage import Store


def document(text='Ada Example\n- Built Python services.\n- Maintained C++ tools.'):
    lines, offset = [], 0
    for index, line in enumerate(text.split('\n')):
        end = offset + len(line.encode('utf-16-le')) // 2
        if line:
            lines.append({'page': 1, 'start': offset, 'end': end, 'text': line,
                          'rect': [.1, .1 + index * .05, .7, .04]})
        offset = end + 1
    return {'text': text, 'pages': [{'number': 1, 'width': 612, 'height': 792}], 'lines': lines}


class ResumeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        initialize_workspace(self.root)
        self.store = Store(self.root / 'data/jobflow.sqlite3')
        self.profile = {'name': 'Ada Example', 'skills': ['Python', 'C++']}
        self.source = b'%PDF-1.7\nOriginal file bytes\n%%EOF\n'
        self.resume = perform_batch_action(self.root, self.store, {}, self.profile, {
            'op': 'upload_resume', 'filename': 'original.pdf',
            'content_base64': base64.b64encode(self.source).decode()})['resume']
        self.store.put('jobs', 'role-a', {'id': 'role-a', 'title': 'Python engineer', 'company': 'Example',
                       'description': 'Develop Python services.', 'url': 'https://example.com/jobs/a'})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def action(self, op, **payload):
        return perform_review_action(self.root, self.store, {}, self.profile, {'op': op, **payload})

    def create(self, doc=None, job_id='role-a'):
        return self.action('pdf_review_create', resume_id=self.resume['id'], job_id=job_id,
                           document=doc or document())['review']

    def replacement(self, review, index=1, **changes):
        line = review['document']['lines'][index]
        return {'id': 'edit-one', 'kind': 'replace', 'source': 'manual', 'status': 'pending',
                'start': line['start'], 'end': line['end'], 'before': line['text'],
                'after': '- Built the documented Python services.', 'page': 1,
                'rects': [line['rect']], **changes}

    def update(self, review, annotations):
        return self.action('pdf_review_update', review_id=review['id'], revision=review['revision'],
                           annotations=annotations)['review']

    def test_reopen_deduplicates_draft_and_reads_are_account_scoped(self):
        review = self.create()
        changed = self.update(review, [self.replacement(review)])
        reopened = self.create()
        self.assertEqual(reopened, changed)
        self.assertEqual(len(self.store.all(REVIEW_KIND)), 1)
        self.assertEqual(review_detail(self.root, review['id'])['review'], changed)
        summary = review_list(self.root, self.resume['id'])['reviews'][0]
        self.assertNotIn('document', summary)
        self.assertNotIn('annotations', summary)
        with tempfile.TemporaryDirectory() as other:
            initialize_workspace(other)
            with self.assertRaisesRegex(ValueError, 'does not exist'):
                review_detail(other, review['id'])
            self.assertEqual(review_list(other)['reviews'], [])
        before = self.store.db.execute('SELECT * FROM records ORDER BY kind,id').fetchall()
        review_detail(self.root, review['id'])
        review_list(self.root, self.resume['id'])
        self.assertEqual(before, self.store.db.execute('SELECT * FROM records ORDER BY kind,id').fetchall())

    def test_document_validation_rejects_wrong_text_pages_and_geometry(self):
        for mutate in (
            lambda doc: doc['lines'][0].update(text='Invented text'),
            lambda doc: doc['lines'][0].update(end=999999),
            lambda doc: doc['lines'][0].update(rect=[.9, .1, .7, .1]),
            lambda doc: doc['lines'][0].update(rect=[float('nan'), 0, .5, .5]),
            lambda doc: doc['pages'][0].update(number=2),
            lambda doc: doc['pages'][0].update(width=-1),
            lambda doc: doc.update(lines=[]),
        ):
            with self.subTest(mutate=mutate):
                doc = document()
                mutate(doc)
                with self.assertRaises(ValueError):
                    self.create(doc)
        self.assertEqual(self.store.all(REVIEW_KIND), [])

    def test_update_requires_current_revision_and_preserves_previous_on_failure(self):
        review = self.create()
        changed = self.update(review, [self.replacement(review)])
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.update(review, [])
        bad = self.replacement(changed, before='Invented old text')
        with self.assertRaisesRegex(ValueError, 'matches|match'):
            self.update(changed, [bad])
        self.assertEqual(review_detail(self.root, review['id'])['review'], changed)

    def test_overlapping_replacements_rejected_but_notes_and_rejected_edits_allowed(self):
        review = self.create()
        first = self.replacement(review)
        second = {**first, 'id': 'edit-two'}
        with self.assertRaisesRegex(ValueError, 'overlap'):
            self.update(review, [first, second])
        note = {**first, 'id': 'note-one', 'kind': 'note', 'note': 'Check phrasing against my source.'}
        changed = self.update(review, [first, {**second, 'status': 'rejected'}, note])
        self.assertEqual(changed['pending_count'], 1)
        self.assertEqual(changed['resolved_text'], review['document']['text'])

    def test_utf16_offsets_preserve_astral_characters_and_reject_split_selections(self):
        review = self.create(document('Ada 😀 Example\n- Built Python services.'))
        annotation = self.replacement(review, status='accepted')
        changed = self.update(review, [annotation])
        self.assertEqual(changed['resolved_text'], 'Ada 😀 Example\n- Built the documented Python services.')
        split = {**annotation, 'start': 5, 'end': 6, 'before': 'x'}
        with self.assertRaisesRegex(ValueError, 'complete characters'):
            self.update(changed, [split])

    def test_scanned_pdf_supports_page_notes_without_claiming_text_edits(self):
        review = self.create(document(''))
        note = {'kind': 'note', 'start': 0, 'end': 0, 'before': '', 'note': 'Clarify this section.',
                'page': 1, 'rects': [[.2, .3, .02, .02]], 'status': 'accepted', 'source': 'manual'}
        changed = self.update(review, [note])
        self.assertEqual(changed['resolved_text'], '')
        with self.assertRaisesRegex(ValueError, 'at least one text edit'):
            self.action('pdf_review_save', review_id=changed['id'], revision=changed['revision'], confirmed=True)

    def test_replacements_cannot_cross_page_boundaries(self):
        doc = document()
        doc['pages'].append({'number': 2, 'width': 612, 'height': 792})
        doc['lines'][2]['page'] = 2
        review = self.create(doc)
        first, last = doc['lines'][1:]
        annotation = self.replacement(review, end=last['end'], before=doc['text'][first['start']:last['end']])
        with self.assertRaisesRegex(ValueError, 'one PDF page'):
            self.update(review, [annotation])

    def test_manual_client_cannot_invent_ai_provenance(self):
        review = self.create()
        with self.assertRaisesRegex(ValueError, 'manual provenance'):
            self.update(review, [self.replacement(review, source='ai')])

    def test_save_requires_resolved_edits_and_creates_idempotent_immutable_pdf(self):
        review = self.create()
        review = self.update(review, [self.replacement(review)])
        payload = {'review_id': review['id'], 'revision': review['revision'], 'confirmed': True,
                   'text': review['resolved_text'], 'content_base64': base64.b64encode(b'%PDF-1.7\nClean revision\n%%EOF').decode(),
                   'label': 'Python role revision', 'set_default': True}
        with self.assertRaisesRegex(ValueError, 'Accept or reject'):
            self.action('pdf_review_save', **payload)
        review = self.update(review, [{**review['annotations'][0], 'status': 'accepted'}])
        payload.update(revision=review['revision'], text=review['resolved_text'])
        with self.assertRaisesRegex(ValueError, 'confirm'):
            self.action('pdf_review_save', **{**payload, 'confirmed': False})
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.action('pdf_review_save', **{**payload, 'text': 'Unreviewed content'})
        result = self.action('pdf_review_save', **payload)
        saved = result['resume']
        self.assertEqual(saved['parent_resume_id'], self.resume['id'])
        self.assertEqual(saved['mime'], 'application/pdf')
        self.assertEqual(saved['source']['layout'], 'reflowed')
        self.assertEqual(saved['job_id'], 'role-a')
        self.assertEqual(saved['version'], 2)
        self.assertEqual(resume_library(self.root)['default_resume_id'], saved['id'])
        self.assertEqual(resume_content(self.root, self.resume['id'])[1], self.source)
        retry = self.action('pdf_review_save', **payload)
        self.assertTrue(retry['already_saved'])
        self.assertEqual(retry['resume']['id'], saved['id'])
        self.assertEqual(len(self.store.all(RESUME_KIND)), 2)
        with self.assertRaises(ValueError):
            self.update(result['review'], [])

    def test_revision_transaction_failure_leaves_no_extra_file_or_record(self):
        review = self.create()
        review = self.update(review, [self.replacement(review, status='accepted')])
        before = set((self.root / 'inputs/application-resumes').iterdir())
        with patch.object(self.store, 'put_groups', side_effect=RuntimeError('simulated persistence failure')):
            with self.assertRaises(RuntimeError):
                self.action('pdf_review_save', review_id=review['id'], revision=review['revision'], confirmed=True,
                            text=review['resolved_text'], label='Revision',
                            content_base64=base64.b64encode(b'%PDF-1.7\n%%EOF').decode())
        self.assertEqual(before, set((self.root / 'inputs/application-resumes').iterdir()))
        self.assertEqual(len(self.store.all(RESUME_KIND)), 1)
        self.assertEqual(review_detail(self.root, review['id'])['review']['status'], 'draft')

    def test_suggestions_append_without_overwriting_decisions_or_manual_work(self):
        review = self.create()
        line = review['document']['lines'][1]
        suggestion = {key: line[key] for key in ('start', 'end')}
        suggestion.update(before=line['text'], after='- Built Python services with care.',
                          reason='Focus the existing evidence.', evidence=['Recorded role'], source='local')
        response = {'suggestions': [suggestion], 'method': 'local', 'message': 'One proposal.'}
        with patch('jobflow.resume_ai.suggest_edits', return_value=response):
            result = self.action('pdf_review_suggest', review_id=review['id'], revision=review['revision'])
        self.assertEqual(result['added'], 1)
        review = result['review']
        proposal = review['annotations'][0]
        self.assertEqual(proposal['page'], 1)
        self.assertEqual(proposal['rects'], [line['rect']])
        self.assertEqual(proposal['status'], 'pending')
        review = self.update(review, [{**proposal, 'status': 'rejected'}])
        with patch('jobflow.resume_ai.suggest_edits', return_value=response):
            result = self.action('pdf_review_suggest', review_id=review['id'], revision=review['revision'])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['review']['annotations'][0]['status'], 'rejected')
        edited = {**result['review']['annotations'][0], 'status': 'accepted', 'after': 'A user revision.',
                  'source': 'manual', 'evidence': ['Fake replacement provenance']}
        review = self.update(result['review'], [edited])
        self.assertEqual(review['annotations'][0]['source'], 'local')
        self.assertEqual(review['annotations'][0]['evidence'], ['Recorded role'])
        self.assertTrue(review['annotations'][0]['user_edited'])

    def test_ai_attempts_are_bounded_and_failures_count_without_changing_annotations(self):
        review = self.create()
        with patch('jobflow.resume_ai.capabilities', return_value={'ai_available': False, 'ai_reason': 'Disabled'}):
            with self.assertRaisesRegex(ValueError, 'Disabled'):
                self.action('pdf_review_suggest', review_id=review['id'], revision=review['revision'], mode='ai')
        self.assertEqual(review_detail(self.root, review['id'])['review']['ai_attempts'], 0)
        with patch('jobflow.resume_ai.capabilities', return_value={'ai_available': True}), \
                patch('jobflow.resume_ai.suggest_edits', side_effect=ValueError('Provider unavailable')) as provider:
            for _ in range(3):
                with self.assertRaisesRegex(ValueError, 'Provider unavailable'):
                    self.action('pdf_review_suggest', review_id=review['id'], revision=review['revision'], mode='ai')
            with self.assertRaisesRegex(ValueError, 'limit of three'):
                self.action('pdf_review_suggest', review_id=review['id'], revision=review['revision'], mode='ai')
            self.assertEqual(provider.call_count, 3)
        changed = review_detail(self.root, review['id'])['review']
        self.assertEqual(changed['revision'], review['revision'])
        self.assertEqual(changed['annotations'], [])
        self.assertEqual(changed['ai_attempts'], 3)


if __name__ == '__main__':
    unittest.main()
