"""Synthetic review manifests only; no model, credentials or production state."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import check_upstream_review as subject
from tools.prepare_upstream_evaluation import frozen_cases


class UpstreamReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='upstream-review-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases = frozen_cases()
        self.manifest = {'format': 1, 'cases': self.save('cases.json', self.cases), 'groups': []}
        for group_id in sorted(subject.GROUPS):
            rows = [dict(id=c['id'], input=c, model_turn_completed=True,
                         native_reply='synthetic native', reply='synthetic product') for c in self.cases]
            group = {'id': group_id, 'observations': self.save(group_id + '-obs.json', rows)}
            if group_id in subject.NORMALIZED_GROUPS:
                normalized = dict(summary={'source_sha256': group['observations']['sha256']},
                                  cases=[dict(id=c['id'], model_turn_completed=True,
                                              provider_last_response='synthetic native',
                                              cli_display='synthetic product') for c in self.cases])
                group['normalized'] = self.save(group_id + '-norm.json', normalized)
            axes = {axis: {'status': 'insufficient', 'reason': 'synthetic fixture, no quality claim'}
                    for axis in subject.AXES}
            reviews = [dict(id=c['id'], question_sha256=c['question_sha256'],
                            native_sha256=subject.text_hash('synthetic native'),
                            display_sha256=subject.text_hash('synthetic product'),
                            model_turn_completed=True, native=axes, product=axes,
                            protocol_gap='runtime_status' if c['id'] == 'fields' else None,
                            wechat_display='not_tested', phone_receipt='not_tested',
                            user_goal_completion='not_verified') for c in self.cases]
            group['reviews'] = self.save(group_id + '-review.json', reviews)
            self.manifest['groups'].append(group)
        self.anchors = {g['id']: g['observations']['sha256'] for g in self.manifest['groups']}
        patch = mock.patch.object(subject, 'OBSERVATIONS_SHA256', self.anchors)
        patch.start()
        self.addCleanup(patch.stop)

    def save(self, name, value):
        data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        (self.root / name).write_bytes(data)
        return {'path': name, 'sha256': subject.sha256(data)}

    def change(self, key, edit):
        group = self.manifest['groups'][0]
        spec = group[key]
        value = json.loads((self.root / spec['path']).read_text())
        edit(value)
        group[key] = self.save(spec['path'], value)
        if key == 'observations':
            self.anchors[group['id']] = group[key]['sha256']

    def check(self):
        self.save('manifest.json', self.manifest)
        return subject.check(self.root / 'manifest.json')

    def test_complete_coverage_is_not_acceptance(self):
        result = self.check()
        self.assertEqual(result['reviewed'], 150)
        self.assertEqual(result['real_model_calls'], 0)
        self.assertEqual(result['acceptance'], 'not_established')
        self.assertEqual(result['deployment'], 'not_performed')

    def test_rejects_modified_artifact_without_new_hash(self):
        spec = self.manifest['groups'][0]['observations']
        (self.root / spec['path']).write_text('[]')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.check()

    def test_rejects_another_question_set(self):
        self.manifest['cases']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'frozen r2'):
            self.check()

    def test_rejects_omitted_or_duplicate_groups(self):
        self.manifest['groups'][1] = copy.deepcopy(self.manifest['groups'][0])
        with self.assertRaisesRegex(ValueError, 'five independent'):
            self.check()

    def test_rejects_whole_group_swapping_even_with_updated_artifact_hash(self):
        groups = self.manifest['groups']
        # Synthetic rows are intentionally identical, so create distinct bytes.
        self.change('observations', lambda rows: rows[0].update(synthetic_group='first'))
        self.change('normalized', lambda obj: obj['summary'].update(
            source_sha256=groups[0]['observations']['sha256']))
        groups[1]['observations'] = copy.deepcopy(groups[0]['observations'])
        with self.assertRaisesRegex(ValueError, 'frozen group'):
            self.check()

    def test_rejects_review_completion_changed(self):
        self.change('reviews', lambda rows: rows[0].update(model_turn_completed=False))
        with self.assertRaisesRegex(ValueError, 'changed completion'):
            self.check()

    def test_rejects_normalization_completion_changed(self):
        self.change('normalized', lambda obj: obj['cases'][0].update(model_turn_completed=False))
        with self.assertRaisesRegex(ValueError, 'changed completion'):
            self.check()

    def test_rejects_non_boolean_completion(self):
        self.change('observations', lambda rows: rows[0].update(model_turn_completed=1))
        self.change('normalized', lambda obj: obj['summary'].update(
            source_sha256=self.manifest['groups'][0]['observations']['sha256']))
        with self.assertRaisesRegex(ValueError, 'invalid completion flag'):
            self.check()

    def test_rejects_missing_question_review(self):
        self.change('reviews', lambda rows: rows.pop())
        with self.assertRaisesRegex(ValueError, 'missing or reordered'):
            self.check()

    def test_rejects_changed_input_preference(self):
        self.change('observations', lambda rows: rows[0]['input'].update(preferences=[]))
        with self.assertRaisesRegex(ValueError, 'inputs/order/preferences'):
            self.check()

    def test_rejects_spliced_native_or_product_answer(self):
        for field in ('native_sha256', 'display_sha256'):
            with self.subTest(field=field):
                self.change('reviews', lambda rows: rows[0].update({field: 'best-other-run'}))
                with self.assertRaisesRegex(ValueError, 'answer mismatch'):
                    self.check()
                self.change('reviews', lambda rows: rows[0].update({field: subject.text_hash(
                    'synthetic native' if field == 'native_sha256' else 'synthetic product')}))

    def test_rejects_pending_axes_or_missing_explanation(self):
        for result in ({'status': 'pending', 'reason': 'not reviewed'},
                       {'status': 'passed', 'reason': ' '}):
            self.change('reviews', lambda rows: rows[0]['native'].update(factual_correctness=result))
            with self.assertRaises(ValueError):
                self.check()

    def test_rejects_missing_axis_or_product_layer(self):
        self.change('reviews', lambda rows: rows[0]['product'].pop('evidence_reliability'))
        with self.assertRaisesRegex(ValueError, 'four axes'):
            self.check()

    def test_rejects_unfinished_answer_pass(self):
        self.change('observations', lambda rows: rows[0].update(model_turn_completed=False))
        self.change('normalized', lambda obj: obj['cases'][0].update(model_turn_completed=False))
        group = self.manifest['groups'][0]
        self.change('normalized', lambda obj: obj['summary'].update(
            source_sha256=group['observations']['sha256']))
        self.change('reviews', lambda rows: rows[0].update(model_turn_completed=False))
        self.change('reviews', lambda rows: rows[0]['native']['factual_correctness'].update(status='passed'))
        with self.assertRaisesRegex(ValueError, 'unfinished answer'):
            self.check()

    def test_rejects_fake_workflow_or_device_acceptance(self):
        for field in ('wechat_display', 'phone_receipt', 'user_goal_completion'):
            self.change('reviews', lambda rows: rows[0].update({field: 'verified'}))
            with self.assertRaisesRegex(ValueError, 'offline review'):
                self.check()
            self.change('reviews', lambda rows: rows[0].update({field:
                'not_verified' if field == 'user_goal_completion' else 'not_tested'}))

    def test_rejects_missing_protocol_as_complete(self):
        self.change('reviews', lambda rows: rows[11]['native']['task_completeness'].update(status='passed'))
        with self.assertRaisesRegex(ValueError, 'private protocol'):
            self.check()

    def test_rejects_other_normalization_or_completion(self):
        self.change('normalized', lambda obj: obj['summary'].update(source_sha256='other'))
        with self.assertRaisesRegex(ValueError, 'another group'):
            self.check()

    def test_rejects_omitted_required_normalization(self):
        self.manifest['groups'][0].pop('normalized')
        with self.assertRaisesRegex(ValueError, 'normalization required'):
            self.check()

    def test_rejects_artifact_path_escape(self):
        self.manifest['groups'][0]['observations']['path'] = '../outside.json'
        with self.assertRaisesRegex(ValueError, 'unsafe artifact path'):
            self.check()

    def test_rejects_symlink_artifact(self):
        group = self.manifest['groups'][0]
        original = self.root / group['observations']['path']
        link = self.root / 'link.json'
        link.symlink_to(original)
        group['observations']['path'] = link.name
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.check()

    def test_null_empty_and_whitespace_answers_are_distinct(self):
        self.assertEqual(len({subject.text_hash(value) for value in (None, '', '\n')}), 3)


if __name__ == '__main__':
    unittest.main()
