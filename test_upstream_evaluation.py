"""Contract tests for private comparison inputs; no model or service calls."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import prepare_upstream_evaluation as subject
from tools.check_answer_quality import boundary_questions, natural_questions, questions


class UpstreamCasesTests(unittest.TestCase):
    def test_preserves_all_questions_and_order(self):
        rows = subject.frozen_cases()
        self.assertEqual([(r['id'], r['question']) for r in rows],
                         questions() + natural_questions() + boundary_questions())
        self.assertEqual(len(rows), 30)
        self.assertEqual(len({r['id'] for r in rows}), 30)
        self.assertEqual([sum(r['suite'] == suite for r in rows)
                          for suite in ('core', 'natural', 'boundary')], [13, 9, 8])

    def test_hashes_are_exact_utf8(self):
        for row in subject.frozen_cases():
            self.assertEqual(row['question_sha256'],
                             hashlib.sha256(row['question'].encode()).hexdigest())

    def test_preferences_match_existing_payloads(self):
        for row in subject.frozen_cases():
            if row['id'].endswith('-default'):
                self.assertEqual(row['preferences'], [])
            else:
                expected = 'brief' if row['id'].endswith('-brief') else 'detailed'
                self.assertEqual(row['preferences'], [{'category': 'verbosity', 'value': expected}])

    def test_never_preapproves_answers_or_private_protocol(self):
        for row in subject.frozen_cases():
            self.assertEqual(set(row['review'].values()), {'pending'})
            self.assertEqual(row['protocol_compatibility'], 'pending')
            self.assertEqual(row['user_goal_completion'], 'unreviewed')
            self.assertTrue(row['independent_session'])
        fields = next(r for r in subject.frozen_cases() if r['id'] == 'fields')
        self.assertIn('runtime_status', fields['question'])

    def test_refuses_repo_existing_and_symlink_targets_before_git(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / 'repo'
            repo.mkdir()
            link = Path(temp) / 'link'
            link.symlink_to(repo)
            with mock.patch.object(subject.subprocess, 'run') as run:
                for path in (repo, repo / 'output', Path(temp), link):
                    with self.assertRaises(ValueError):
                        subject.prepare(path, repo)
                run.assert_not_called()

    def test_missing_commit_does_not_create_report(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / 'new'
            with mock.patch.object(subject.subprocess, 'run', side_effect=RuntimeError('missing')):
                with self.assertRaises(RuntimeError):
                    subject.prepare(report)
            self.assertFalse(report.exists())

    def test_json_preserves_question_unicode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'cases.json'
            subject.write_json(path, subject.frozen_cases())
            self.assertEqual(json.loads(path.read_text()), subject.frozen_cases())
            with self.assertRaises(FileExistsError):
                subject.write_json(path, [])


if __name__ == '__main__':
    unittest.main()
