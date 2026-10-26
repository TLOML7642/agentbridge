"""Release preconditions on private synthetic files, never remote side effects."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import release_experience as release


class TestReleasePreconditions(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.live, self.stage = root / 'live', root / 'stage'
        self.live.mkdir()
        self.stage.mkdir()
        mapping = {name: self.live / name for name in ('shim.py', 'main.py')}
        for name, path in mapping.items():
            path.write_text('VERSION = 1\n')
            path.chmod(0o600)
            (self.stage / name).write_text('VERSION = 2\n')
        (self.live / 'compose.yaml').write_text('services: {}\n')
        protected = self.live / 'protected-config'
        protected.write_text('synthetic\n')
        self.rows = [{'Name': '/agentbridge-astrbot', 'Image': 'synthetic-image-a',
                      'State': {'Running': True}, 'Config': {'Env': []}},
                     {'Name': '/agentbridge-shim', 'Image': 'synthetic-image-b',
                      'State': {'Running': True}, 'Config': {'Env': []}}]
        for key, value in (('ROOT', self.live), ('STAGE', self.stage), ('BACKUP', root / 'backup'),
                           ('MAPPING', mapping), ('PROTECTED', [protected]),
                           ('EXPECTED', {name: release.digest(path) for name, path in mapping.items()})):
            stack.enter_context(mock.patch.object(release, key, value))
        stack.enter_context(mock.patch.object(release, 'containers', side_effect=lambda: copy.deepcopy(self.rows)))
        (self.stage / 'sha256.json').write_text(json.dumps({name: release.digest(self.stage / name) for name in mapping}))
        release.prepare()

    def test_install_requires_stopped_intake_and_preserves_permissions(self):
        with self.assertRaises(AssertionError):
            release.install()
        self.assertEqual((self.live / 'shim.py').read_text(), 'VERSION = 1\n')
        self.rows[0]['State']['Running'] = False
        release.install()
        for name in ('shim.py', 'main.py'):
            self.assertEqual((self.live / name).read_text(), 'VERSION = 2\n')
            self.assertEqual((self.live / name).stat().st_mode & 0o777, 0o600)

    def test_concurrent_config_or_source_edit_aborts_before_any_copy(self):
        self.rows[0]['State']['Running'] = False
        (self.live / 'main.py').write_text('VERSION = 3\n')
        with self.assertRaises(AssertionError):
            release.install()
        self.assertEqual((self.live / 'shim.py').read_text(), 'VERSION = 1\n')
        (self.live / 'main.py').write_text('VERSION = 1\n')
        (self.live / 'protected-config').write_text('changed concurrently\n')
        with self.assertRaises(AssertionError):
            release.install()
        self.assertEqual((self.live / 'shim.py').read_text(), 'VERSION = 1\n')


if __name__ == '__main__':
    unittest.main()
