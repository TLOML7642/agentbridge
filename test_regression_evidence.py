import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class EvidenceTests(unittest.TestCase):
    def test_interrupted_runner_preserves_registered_scene(self):
        from tools.regression_evidence import preserve_interrupted
        with tempfile.TemporaryDirectory(prefix='ab-evidence-test-') as scratch:
            root = Path(scratch)
            scene, report = root / 'scene', root / 'report'
            scene.mkdir()
            report.mkdir()
            (scene / 'receipt.json').write_text('{"synthetic": true}')
            (report / 'fixture-paths.jsonl').write_text(json.dumps({'test': 'synthetic.interrupted', 'path': str(scene)}) + '\n')
            preserve_interrupted(report)
            self.assertTrue(list(report.glob('interrupted/snapshot-*/receipt.json')))
            self.assertTrue((scene / 'receipt.json').exists())

    def test_failures_and_subtests_retain_consistent_databases_before_cleanup(self):
        with tempfile.TemporaryDirectory(prefix='ab-evidence-test-') as scratch:
            root = Path(scratch)
            fixture = root / 'test_fixture.py'
            fixture.write_text('''import unittest, tempfile, sqlite3
from pathlib import Path
class Fixture(unittest.TestCase):
    def test_failure(self):
        with tempfile.TemporaryDirectory() as path:
            db = sqlite3.connect(Path(path) / 'synthetic.db')
            db.execute('CREATE TABLE marker(value TEXT)')
            db.execute("INSERT INTO marker VALUES ('synthetic')")
            db.commit()
            db.close()
            (Path(path) / 'receipt.json').write_text('{"synthetic": true}')
            (Path(path) / 'ignore-link').symlink_to('/etc/hosts')
        self.fail('intentional synthetic failure after cleanup')
    def test_subtest(self):
        with tempfile.TemporaryDirectory() as path:
            (Path(path) / 'subtest.json').write_text('{}')
            with self.subTest(phase='synthetic'):
                self.assertTrue(False, 'intentional synthetic subtest failure')
''')
            report = root / 'report'
            runner = Path(__file__).parent / 'tools/regression_evidence.py'
            result = subprocess.run([sys.executable, '-B', str(runner), str(report), 'test_fixture'],
                                    cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            summary = json.loads((report / 'summary.json').read_text())
            self.assertEqual((summary['tests'], summary['failures'], summary['errors']), (2, 2, 0))
            self.assertTrue(list(report.glob('fixture-*/snapshot-*/synthetic.db')))
            self.assertTrue(list(report.glob('fixture-*/snapshot-*/receipt.json')))
            self.assertTrue(list(report.glob('fixture-*/snapshot-*/subtest.json')))
            self.assertFalse(list(report.glob('fixture-*/snapshot-*/ignore-link')))
            for line in (report / 'fixture-paths.jsonl').read_text().splitlines():
                self.assertFalse(Path(json.loads(line)['path']).exists())
            for manifest in report.glob('fixture-*/snapshot-*/manifest.json'):
                self.assertFalse(any('error_type' in value for value in json.loads(manifest.read_text())['files'].values()))

    def test_success_does_not_retain_synthetic_payloads(self):
        with tempfile.TemporaryDirectory(prefix='ab-evidence-test-') as scratch:
            root = Path(scratch)
            (root / 'test_fixture.py').write_text('''import tempfile, unittest
from pathlib import Path
class Fixture(unittest.TestCase):
    def test_ok(self):
        with tempfile.TemporaryDirectory() as path:
            (Path(path) / 'receipt.json').write_text('{}')
''')
            report = root / 'report'
            result = subprocess.run([sys.executable, '-B', str(Path(__file__).parent / 'tools/regression_evidence.py'),
                                     str(report), 'test_fixture'], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(report.glob('fixture-[0-9]*')), [])
