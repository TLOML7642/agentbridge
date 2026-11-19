"""Synthetic user homes only. Never launch an IDE or trash personal files."""
import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
import struct
import zlib
import subprocess
from unittest import mock

import computer_actions as actions
from agent_approvals import fingerprint
from agent_protocol import bound_trash_progress
from agent_runtime import AgentError
from agent_tasks import TaskRuntime
import test_agent_runtime as chat_tests


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1sAAAAASUVORK5CYII=')


def synthetic_trash(root, staged):
    trash = Path(root) / '.Trash'
    trash.mkdir(mode=0o700, exist_ok=True)
    destination = trash / staged.name
    assert not os.path.lexists(destination)
    staged.rename(destination)
    return destination


def synthetic_exclusive(source_fd, source, target_fd, target):
    try:
        os.stat(target, dir_fd=target_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(source, target, src_dir_fd=source_fd, dst_dir_fd=target_fd)
    else:
        raise FileExistsError(target)


class ComputerFiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='computer-synthetic-home-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'project/demo').mkdir(parents=True)
        (self.root / 'project/demo/.idea').mkdir()
        (self.root / 'Pictures').mkdir()
        (self.root / 'Pictures/input.png').write_bytes(PNG)

    def prepared(self, tool, args):
        private = self.root / '.runtime'
        prefixes = [str(private)]
        return {'root': str(self.root), 'tool': tool, 'arguments': actions.prepare(str(self.root), tool, args, prefixes),
                'private_prefixes': prefixes, 'journal': str(private / 'action-evidence/journal.jsonl')}

    def test_projects_are_discovered_by_folder_without_registration(self):
        (self.root / 'project/second').mkdir()
        (self.root / 'project/.private').mkdir()
        (self.root / 'project/escape').symlink_to(self.root.parent)
        result = actions.inspect(str(self.root), 'projects', '')
        self.assertEqual([item['name'] for item in result['projects']], ['demo', 'second'])
        self.assertTrue(result['projects'][0]['pycharm_project'])

    def test_directory_identity_uses_portable_nofollow_stat(self):
        directory = self.root / 'project/demo'
        expected = directory.lstat()
        self.assertEqual(actions.directory_identity(directory), [expected.st_dev, expected.st_ino])
        link = self.root / 'directory-link'
        link.symlink_to(directory)
        with self.assertRaises(ValueError):
            actions.directory_identity(link)
        with self.assertRaises(ValueError):
            actions.directory_identity(self.root / 'Pictures/input.png')

    def test_home_alias_is_bound_to_explicit_root_not_process_home(self):
        with mock.patch.dict(os.environ, {'HOME': '/synthetic-server-home'}):
            self.assertEqual(actions.locate(self.root, '~/Pictures/input.png'), self.root / 'Pictures/input.png')
        self.assertEqual(actions.locate(self.root, '~/Pictures/new.png', absent=True), self.root / 'Pictures/new.png')
        for path in ('~', '~other/Pictures', '~/../outside', '~/.ssh/id', '~/Library', '~/Pictures/input.png'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                actions.locate(self.root, path, absent=True)
        with self.assertRaises(ValueError):
            self.prepared('computer_trash_files', {'paths': json.dumps(['Pictures/input.png', '~/Pictures/input.png'])})

    def test_paths_protect_credentials_symlinks_runtime_and_existing_outputs(self):
        (self.root / '.ssh').mkdir()
        (self.root / '.ssh/id').write_text('synthetic credential')
        (self.root / 'Library').mkdir()
        (self.root / 'escape').symlink_to(self.root / 'Pictures')
        for path in ('../outside', '/', '.ssh/id', 'Library', 'escape/input.png', 'project/demo/../../.ssh/id',
                     'Pictures/credentials.json', 'Pictures/private.key', 'Pictures/chat.db'):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                actions.locate(self.root, path)
        with self.assertRaises(ValueError):
            actions.locate(self.root, 'Pictures/input.png', [str(self.root / 'Pictures')])
        with self.assertRaises(ValueError):
            self.prepared('computer_resize_image', {'source': 'Pictures/input.png', 'destination': 'Pictures/input.png', 'max_pixels': '200'})
        with self.assertRaises(ValueError):
            self.prepared('computer_trash_file', {'path': 'project/demo'})

    def test_frozen_input_change_is_rejected_before_any_effect(self):
        request = self.prepared('computer_trash_file', {'path': 'Pictures/input.png'})
        (self.root / 'Pictures/input.png').write_bytes(b'changed synthetic file')
        with mock.patch.object(actions.sys, 'platform', 'darwin'), mock.patch.object(actions, 'move_exclusive') as move:
            with self.assertRaises(ValueError):
                actions.execute(request)
            move.assert_not_called()
        self.assertTrue((self.root / 'Pictures/input.png').exists())

    def test_trash_move_retains_synthetic_bytes_and_is_not_permanent_delete(self):
        request = self.prepared('computer_trash_file', {'path': 'Pictures/input.png'})
        with mock.patch.object(actions.sys, 'platform', 'darwin'), \
                mock.patch.object(actions, 'move_exclusive', synthetic_exclusive), \
                mock.patch.object(actions, 'foundation_trash', synthetic_trash):
            result = actions.execute(request)
        self.assertFalse(result['permanently_deleted'])
        self.assertEqual(Path(result['trash_path']).read_bytes(), PNG)
        self.assertFalse((self.root / 'Pictures/input.png').exists())
        self.assertEqual(result['completion'], 'verified')

    def test_trash_collision_or_cross_volume_failure_never_removes_source(self):
        request = self.prepared('computer_trash_file', {'path': 'Pictures/input.png'})
        with mock.patch.object(actions.sys, 'platform', 'darwin'), \
                mock.patch.object(actions, 'move_exclusive', side_effect=OSError('synthetic move failure')), \
                mock.patch.object(actions, 'foundation_trash') as recycle:
            result = actions.execute(request)
            recycle.assert_not_called()
        self.assertEqual(result['completion'], 'uncertain')
        self.assertEqual(result['moved'], [])
        self.assertEqual((self.root / 'Pictures/input.png').read_bytes(), PNG)

    def test_replacement_at_staging_rename_is_not_sent_to_trash(self):
        source = self.root / 'Pictures/input.png'
        saved = source.with_name('approved-original.png')
        request = self.prepared('computer_trash_file', {'path': 'Pictures/input.png'})
        def substitute_once(source_fd, leaf, target_fd, target):
            if not saved.exists():
                source.rename(saved)
                source.write_bytes(b'unapproved replacement')
            synthetic_exclusive(source_fd, leaf, target_fd, target)
        with mock.patch.object(actions.sys, 'platform', 'darwin'), \
                mock.patch.object(actions, 'move_exclusive', substitute_once), \
                mock.patch.object(actions, 'foundation_trash') as recycle:
            result = actions.execute(request)
            recycle.assert_not_called()
        self.assertEqual(result['completion'], 'uncertain')
        self.assertEqual(result['needs_check'], 'Pictures/input.png')
        self.assertEqual(saved.read_bytes(), PNG)
        self.assertEqual(source.read_bytes(), b'unapproved replacement')
        records = [json.loads(line) for line in Path(request['journal']).read_text().splitlines()]
        self.assertEqual([row['stage'] for row in records], ['trash-stage-intent', 'trash-stage-restored'])

    def test_rollback_collision_never_overwrites_or_trashes_either_replacement(self):
        source = self.root / 'Pictures/input.png'
        saved = source.with_name('approved-original.png')
        request = self.prepared('computer_trash_file', {'path': 'Pictures/input.png'})
        def substitute_twice(source_fd, leaf, target_fd, target):
            if not saved.exists():
                source.rename(saved)
                source.write_bytes(b'first unapproved replacement')
                synthetic_exclusive(source_fd, leaf, target_fd, target)
                source.write_bytes(b'second unapproved replacement')
                return
            synthetic_exclusive(source_fd, leaf, target_fd, target)
        with mock.patch.object(actions.sys, 'platform', 'darwin'), \
                mock.patch.object(actions, 'move_exclusive', substitute_twice), \
                mock.patch.object(actions, 'foundation_trash') as recycle:
            result = actions.execute(request)
            recycle.assert_not_called()
        self.assertEqual(result['completion'], 'uncertain')
        self.assertEqual(saved.read_bytes(), PNG)
        self.assertEqual(source.read_bytes(), b'second unapproved replacement')
        record = json.loads(Path(request['journal']).read_text())
        self.assertEqual(record['stage'], 'trash-stage-intent')
        self.assertEqual(Path(record['staged_path']).read_bytes(), b'first unapproved replacement')

    def test_system_recycle_sees_only_the_bound_file_after_public_path_changes(self):
        source = self.root / 'Pictures/input.png'
        request = self.prepared('computer_trash_file', {'path': 'Pictures/input.png'})
        def replace_public_entry(root, staged):
            self.assertNotEqual(staged, source)
            self.assertEqual(staged.read_bytes(), PNG)
            self.assertFalse(source.exists())
            source.write_bytes(b'new public entry left alone')
            return synthetic_trash(root, staged)
        with mock.patch.object(actions.sys, 'platform', 'darwin'), \
                mock.patch.object(actions, 'move_exclusive', synthetic_exclusive), \
                mock.patch.object(actions, 'foundation_trash', replace_public_entry):
            result = actions.execute(request)
        self.assertEqual(result['completion'], 'verified')
        self.assertEqual(Path(result['trash_path']).read_bytes(), PNG)
        self.assertEqual(source.read_bytes(), b'new public entry left alone')

    def test_resize_creates_new_copy_and_keeps_source(self):
        request = self.prepared('computer_resize_image', {'source': 'Pictures/input.png',
            'destination': 'Pictures/output.png', 'max_pixels': '200'})
        with mock.patch.object(actions.sys, 'platform', 'darwin'), \
                mock.patch.object(actions.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
            result = actions.execute(request)
        self.assertEqual((self.root / 'Pictures/input.png').read_bytes(), PNG)
        self.assertEqual((self.root / 'Pictures/output.png').read_bytes(), PNG)
        self.assertTrue(result['original_preserved'])
        self.assertNotIn(str(self.root / 'Pictures/input.png'), run.call_args.args[0])

    def test_pycharm_open_is_not_misrepresented_as_project_running(self):
        request = self.prepared('computer_open_project', {'project': 'demo'})
        with mock.patch.object(actions.sys, 'platform', 'darwin'), \
                mock.patch.object(actions.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
            result = actions.execute(request)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '-a', 'PyCharm', str(self.root / 'project/demo')])
        self.assertFalse(result['project_running_verified'])
        self.assertEqual(result['completion'], 'process-ended')

    def test_batch_trash_is_one_frozen_manifest_without_recursive_delete(self):
        (self.root / 'Pictures/second.png').write_bytes(PNG)
        request = self.prepared('computer_trash_files', {'paths': json.dumps(['Pictures/input.png', 'Pictures/second.png'])})
        with mock.patch.object(actions.sys, 'platform', 'darwin'), \
                mock.patch.object(actions, 'move_exclusive', synthetic_exclusive), \
                mock.patch.object(actions, 'foundation_trash', synthetic_trash):
            result = actions.execute(request)
        self.assertEqual(result['count'], 2)
        self.assertFalse(result['permanently_deleted'])
        self.assertEqual([Path(item['trash_path']).read_bytes() for item in result['files']], [PNG, PNG])
        self.assertFalse((self.root / 'Pictures/input.png').exists())
        self.assertFalse((self.root / 'Pictures/second.png').exists())

    def test_batch_target_change_rejects_whole_batch_before_first_move(self):
        (self.root / 'Pictures/second.png').write_bytes(PNG)
        request = self.prepared('computer_trash_files', {'paths': json.dumps(['Pictures/input.png', 'Pictures/second.png'])})
        (self.root / 'Pictures/second.png').write_bytes(b'changed file')
        with mock.patch.object(actions.sys, 'platform', 'darwin'), mock.patch.object(actions, 'move_exclusive') as move:
            with self.assertRaises(ValueError):
                actions.execute(request)
            move.assert_not_called()
        self.assertEqual((self.root / 'Pictures/input.png').read_bytes(), PNG)

    def test_batch_limits_are_not_automatically_split_and_partial_moves_are_journaled(self):
        paths = []
        for index in range(21):
            name = 'Pictures/file-' + str(index)
            (self.root / name).write_text('synthetic')
            paths.append(name)
        with self.assertRaises(ValueError):
            self.prepared('computer_trash_files', {'paths': json.dumps(paths)})
        request = self.prepared('computer_trash_files', {'paths': json.dumps(paths[:3])})
        calls = []
        def partial(root, staged):
            calls.append(staged.name)
            if len(calls) == 2:
                raise OSError('synthetic interrupted batch')
            return synthetic_trash(root, staged)
        with mock.patch.object(actions.sys, 'platform', 'darwin'), \
                mock.patch.object(actions, 'move_exclusive', synthetic_exclusive), \
                mock.patch.object(actions, 'foundation_trash', partial):
            result = actions.execute(request)
        self.assertEqual(result['completion'], 'uncertain')
        self.assertEqual(result['moved'], paths[:1])
        self.assertEqual(result['needs_check'], paths[1])
        self.assertEqual(result['not_attempted'], paths[2:3])
        self.assertEqual(calls, ['file-0', 'file-1'])
        records = [json.loads(line) for line in Path(request['journal']).read_text().splitlines()]
        self.assertEqual([row['stage'] for row in records], ['trash-stage-intent', 'trash-intent', 'trash-verified',
                                                           'trash-stage-intent', 'trash-intent', 'trash-stage-restored'])
        self.assertEqual(Path(records[2]['trash_path']).read_text(), 'synthetic')
        self.assertTrue((self.root / paths[1]).exists())
        self.assertTrue((self.root / paths[2]).exists())

    def test_rotation_and_crop_are_fixed_options_not_shell_commands(self):
        for operation, value, expected in [('rotate', '90', ['-r', '90']), ('crop', '100x200', ['--cropToHeightWidth', '100', '200'])]:
            self.assertEqual(actions.image_flags('computer_edit_image', {'operation': operation, 'value': value}), expected)
        for operation, value in [('shell', 'rm'), ('rotate', '90; rm'), ('crop', '-1x10'), ('resize', '0')]:
            with self.assertRaises(ValueError):
                actions.image_flags('computer_edit_image', {'operation': operation, 'value': value})

    def test_actual_macos_sips_and_exclusive_staging_use_only_synthetic_home(self):
        if sys.platform != 'darwin':
            return  # Other platforms exercise the synthetic adapter, not Mac APIs.
        request = self.prepared('computer_resize_image', {'source': 'Pictures/input.png',
            'destination': 'Pictures/resized.png', 'max_pixels': '200'})
        result = actions.execute(request)
        self.assertTrue(result['original_preserved'])
        self.assertEqual((self.root / 'Pictures/input.png').read_bytes(), PNG)
        self.assertTrue((self.root / 'Pictures/resized.png').stat().st_size > 0)
        request = self.prepared('computer_trash_file', {'path': 'Pictures/resized.png'})
        with mock.patch.object(actions, 'foundation_trash', synthetic_trash):
            result = actions.execute(request)
        self.assertTrue(Path(result['trash_path']).is_file())
        self.assertFalse((self.root / 'Pictures/resized.png').exists())

    def test_actual_macos_rotation_crop_and_resize_dimensions_preserve_original(self):
        if sys.platform != 'darwin':
            return
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        data = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 8, 6, 8, 2, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress((b'\0' + b'\x30\x80\xc0' * 8) * 6)) + chunk(b'IEND', b''))
        source = self.root / 'Pictures/input.png'
        source.write_bytes(data)
        for operation, value, expected in [('rotate', '90', (6, 8)), ('crop', '2x4', (4, 2)), ('resize', '4', (4, 3))]:
            destination = 'Pictures/' + operation + '.png'
            result = actions.execute(self.prepared('computer_edit_image', {'source': '~/Pictures/input.png',
                'destination': '~/' + destination, 'operation': operation, 'value': value}))
            output = subprocess.check_output(['/usr/bin/sips', '-g', 'pixelWidth', '-g', 'pixelHeight', str(self.root / destination)], text=True)
            dimensions = tuple(int(line.split(':')[1]) for line in output.splitlines() if 'pixelWidth:' in line or 'pixelHeight:' in line)
            self.assertEqual(dimensions, expected)
            self.assertEqual(source.read_bytes(), data)
            self.assertTrue(result['original_preserved'])

    def configuration(self):
        directory = self.root / 'project/demo/.idea/runConfigurations'
        directory.mkdir()
        path = directory / 'demo.xml'
        path.write_text('<component><configuration name="demo-run" type="PythonConfigurationType">'
                        '<option name="SCRIPT_NAME" value="$PROJECT_DIR$/main.py"/>'
                        '<option name="SDK_HOME" value="' + sys.executable + '"/></configuration></component>')
        (self.root / 'project/demo/main.py').write_text('print("synthetic")')
        return path

    def test_existing_run_configuration_is_discovered_not_arbitrary_script(self):
        self.configuration()
        info = actions.inspect(str(self.root), 'configurations', 'demo')
        self.assertEqual(info['configurations'], [{'name': 'demo-run', 'type': 'PythonConfigurationType'}])
        request = self.prepared('computer_run_project', {'project': 'demo', 'configuration': 'demo-run'})
        with mock.patch.object(actions.sys, 'platform', 'darwin'), \
                mock.patch.object(actions.subprocess, 'run', return_value=types.SimpleNamespace(
                    returncode=0, stdout=b'{"ok":true,"stage":"run_requested"}')) as run:
            result = actions.execute(request)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-3:], ['demo', 'demo-run', str(self.root / 'project/demo')])
        self.assertEqual(run.call_args.args[0][:5], ['/usr/bin/osascript', '-l', 'JavaScript', '-e', actions.PYCHARM_RUN])
        self.assertFalse(result['project_running_verified'])
        self.assertFalse(result['user_goal_verified'])
        self.assertEqual(run.call_args.kwargs['timeout'], 15)

    def test_jxa_checks_exact_path_localized_menu_and_dialogs_not_title_identity(self):
        node = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE')
        if not node:
            self.skipTest('explicit native test runtime required')
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const context = {};
vm.runInNewContext(require('node:fs').readFileSync(0, 'utf8'), context);
const root = '/synthetic/project/demo';
function fixture(options={}) {
  let menuOpened = false, runClicks = 0, raiseCount = 0;
  function window(path=root, description='项目: demo，菜单') {
    const button = {properties:()=>({role:'AXButton',description,help:path})};
    let group = {role:()=> 'AXGroup',uiElements:()=>[button]};
    for(let i=0;i<3;i++){const child=group;group={role:()=> 'AXGroup',uiElements:()=>[child]};}
    if(options.notification){const child=group.uiElements()[0];group={role:()=> 'AXGroup',
      uiElements:()=>[{role:()=> 'AXScrollArea',uiElements:()=>[]},child]};}
    return {name:()=>options.dialogName ?? 'untrusted window title',uiElements:()=>[group],
      attributes:{byName:name=>({value:()=>{assert.equal(name,'AXModal');
        return options.modal ?? (options.popupAfterMenu && menuOpened) ?? false;}})},
      sheets:()=>options.sheet?[{}]:[],subrole:()=>options.subrole ?? 'AXStandardWindow',
      actions:{byName:name=>({perform:()=>{assert.equal(name,'AXRaise');raiseCount++;}})}};
  }
  const target=window(options.path ?? root,options.description);
  let windows=options.duplicate?[target,window()]:[target];
  const runItem={name:()=>options.item ?? (options.english?"Run 'demo-run'":"运行 'demo-run'"),
    enabled:()=>options.enabled ?? true,click:()=>{runClicks++;}};
  const menu={name:()=>options.english?'Run':'运行',click:()=>{menuOpened=true;},
    menus:()=>menuOpened?[{menuItems:()=>options.duplicateItem?[runItem,runItem]:[runItem]}]:[]};
  let focusReads=0;
  const ide={windows:()=>raiseCount&&(options.frontChanged||(options.delayedFocus&&focusReads++<3))?[window('/other')]:windows,
    menuBars:()=>[{menuBarItems:()=>[menu]}]};
  return {ide,counts:()=>({runClicks,menuOpened,raiseCount})};
}
for(const options of [{},{english:true,description:'Project: demo, menu'},{notification:true},{delayedFocus:true}]){
  const f=fixture(options);context.inspectRun(f.ide,'demo',root);
  assert.deepEqual(f.counts(),{runClicks:0,menuOpened:false,raiseCount:0});
  const result=context.requestRun(f.ide,'demo','demo-run',root);
  assert.equal(result.stage,'run_requested');assert.equal(f.counts().runClicks,1);assert.equal(f.counts().menuOpened,true);
}
for(const [options,error] of [
  [{path:'/other'},'PROJECT_IDENTITY_UNAVAILABLE'],
  [{path:'/other',dialogName:'demo'},'PROJECT_IDENTITY_UNAVAILABLE'],
  [{path:root+'/nested'},'PROJECT_IDENTITY_UNAVAILABLE'],
  [{path:root+'-lookalike'},'PROJECT_IDENTITY_UNAVAILABLE'],
  [{description:'untrusted editor tooltip'},'PROJECT_IDENTITY_UNAVAILABLE'],
  [{modal:true},'UI_BLOCKED'],
  [{sheet:true},'UI_BLOCKED'],
  [{popupAfterMenu:true},'UI_BLOCKED'],
  [{subrole:'AXDialog'},'UI_BLOCKED'],
  [{dialogName:'运行/调试配置'},'UI_BLOCKED'],
  [{dialogName:'Run/Debug Configurations'},'UI_BLOCKED'],
  [{duplicate:true},'PROJECT_IDENTITY_AMBIGUOUS'],
  [{frontChanged:true},'FRONT_PROJECT_CHANGED'],
  [{item:"运行 'other'"},'RUN_CONFIGURATION_UNAVAILABLE'],
  [{item:"调试 'demo-run'"},'RUN_CONFIGURATION_UNAVAILABLE'],
  [{duplicateItem:true},'RUN_CONFIGURATION_UNAVAILABLE'],
  [{enabled:false},'RUN_CONFIGURATION_DISABLED']]){
  const f=fixture(options);assert.throws(()=>context.requestRun(f.ide,'demo','demo-run',root),new RegExp(error));
  assert.equal(f.counts().runClicks,0);
}
process.stdout.write('project identity and localized menu policy passed');
'''
        result = subprocess.run([node, '-e', script], input=actions.PYCHARM_RUN, capture_output=True,
                                text=True, env={**os.environ, 'ELECTRON_RUN_AS_NODE': '1'}, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_interpreter_refuses_before_any_ui_action(self):
        path = self.configuration()
        path.write_text(path.read_text().replace(sys.executable, str(self.root / 'missing-python')))
        with mock.patch.object(actions.subprocess, 'run') as run:
            with self.assertRaises((ValueError, OSError)):
                self.prepared('computer_run_project', {'project': 'demo', 'configuration': 'demo-run'})
            run.assert_not_called()

    def test_unmatched_ui_receipt_never_reports_run_requested(self):
        self.configuration()
        request = self.prepared('computer_run_project', {'project': 'demo', 'configuration': 'demo-run'})
        for stdout in (b'', b'not-json', b'[]', b'null', b'{"code":[]}', b'{"ok":true}',
                       b'{"ok":false,"code":"PROJECT_IDENTITY_UNAVAILABLE"}',
                       b'{"ok":false,"code":"UI_BLOCKED"}'):
            with mock.patch.object(actions.sys, 'platform', 'darwin'), \
                    mock.patch.object(actions.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=stdout)):
                result = actions.execute(request)
            self.assertEqual(result['evidence'], 'pycharm_run_not_confirmed')
            self.assertNotEqual(result['exit_code'], 0)
            if b'UI_BLOCKED' in stdout:
                self.assertEqual(result['ui_code'], 'UI_BLOCKED')

    def test_changed_ambiguous_or_symlinked_configuration_does_not_run(self):
        path = self.configuration()
        request = self.prepared('computer_run_project', {'project': 'demo', 'configuration': 'demo-run'})
        path.write_text(path.read_text().replace('main.py', 'changed.py'))
        with mock.patch.object(actions.sys, 'platform', 'darwin'), mock.patch.object(actions.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                actions.execute(request)
            run.assert_not_called()
        other = path.with_name('duplicate.xml')
        other.write_bytes(path.read_bytes())
        with self.assertRaises(ValueError):
            self.prepared('computer_run_project', {'project': 'demo', 'configuration': 'demo-run'})
        with self.assertRaises(ValueError):
            self.prepared('computer_run_project', {'project': 'demo', 'configuration': 'python arbitrary.py'})

    def test_native_task_automatic_copy_uses_real_boundary_without_wechat_prompt(self):
        if sys.platform != 'darwin':
            return
        from agent_task_executor import NativeTaskExecutor
        from process_supervisor import run_task, _executor_identity
        node, packages = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE'), os.environ.get('AGENTBRIDGE_TEST_NATIVE_PACKAGES')
        if not node or not packages:
            self.skipTest('explicit pinned native runtime required')
        executor = NativeTaskExecutor(self.root / '.runtime/tasks/native', run_task, _executor_identity, lambda _: 'dead')
        runtime = TaskRuntime(self.root / '.runtime/tasks', executor, lambda _: str(self.root / 'project/demo'),
                              enabled=True, allowed_projects={'legacy'}, computer_root=str(self.root))
        self.addCleanup(runtime.close)
        subject = dict(sender='owner', channel='private', conversation='original', project='assistant',
                       cwd=str(self.root), session=uuid.uuid4().hex, attempt=uuid.uuid4().hex)
        with mock.patch.dict(os.environ, {'AGENTBRIDGE_AGENT_NODE': node, 'AGENTBRIDGE_AGENT_PACKAGES': packages}):
            task = runtime.submit(subject, 'native-copy', 'computer_resize_image', {'source': '~/Pictures/input.png',
                'destination': '~/Pictures/native.png', 'max_pixels': '200'})
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                result = runtime.inspect('owner', 'private', 'original', 'assistant', task['task_id'])
                if result['state'] in ('verified', 'uncertain', 'failed', 'denied'):
                    break
                time.sleep(.02)
            self.assertEqual(result['state'], 'verified')
        self.assertEqual((self.root / 'Pictures/input.png').read_bytes(), PNG)
        self.assertTrue((self.root / 'Pictures/native.png').is_file())
        self.assertNotIn('approval', [event['kind'] for event in runtime.events('owner', 'private', 'original', 'assistant')['events']])
        self.assertEqual(runtime.submit(subject, 'native-copy', 'computer_resize_image', {'source': '~/Pictures/input.png',
            'destination': '~/Pictures/native.png', 'max_pixels': '200'})['task_id'], task['task_id'])


class ComputerPolicy(unittest.TestCase):
    def setUp(self):
        ComputerFiles.setUp(self)
        self.calls = []
        self.ready = threading.Event()
        self.addCleanup(self.ready.set)
        def runner(task, cancel):
            self.calls.append(task)
            self.ready.wait(3)
            return {'completion': 'verified', 'evidence': 'synthetic-only'}
        self.runtime = TaskRuntime(self.root / '.runtime/tasks', runner, lambda _: str(self.root / 'project/demo'),
            enabled=True, allowed_projects={'legacy'}, computer_root=str(self.root))
        self.addCleanup(self.runtime.close)
        self.subject = {'sender': 'owner', 'channel': 'private', 'conversation': 'original', 'project': 'assistant',
                        'cwd': str(self.root), 'session': uuid.uuid4().hex, 'attempt': uuid.uuid4().hex}

    def request(self, tool, args):
        row = self.runtime.submit(self.subject, uuid.uuid4().hex, tool, args)
        deadline = time.monotonic() + 3
        while not self.calls and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.calls)
        task = self.calls[-1]
        request = dict(version=1, epoch='native-epoch', execution='native-execution', subject=task['subject'],
                       tool=tool, arguments=json.loads(task['arguments']), callId=row['task_id'], rootCallId=row['task_id'], nested=False)
        request['digest'] = fingerprint(request)
        broker = task['broker']
        return row, request, broker

    def test_safe_task_gets_host_grant_and_single_consumption_without_user_prompt(self):
        row, request, broker = self.request('computer_open_project', {'project': 'demo'})
        answer = self.runtime.rpc({'capability': broker['capability'], 'op': 'approval', 'request': request})
        self.assertEqual(answer['state'], 'approved')
        consume = {'capability': broker['capability'], 'op': 'consume', 'request': request, 'grant': answer['grant']}
        self.assertTrue(self.runtime.rpc(consume)['consumed'])
        self.assertFalse(self.runtime.rpc(consume)['consumed'])
        events = self.runtime.events('owner', 'private', 'original', 'assistant')['events']
        self.assertNotIn('approval', [event['kind'] for event in events])
        with self.runtime.approvals.db() as db:
            self.assertTrue(db.execute('SELECT delivery FROM decisions').fetchone()[0].startswith('host-auto:'))

    def test_trash_requires_exact_user_decision_and_changed_params_do_not_consume(self):
        row, request, broker = self.request('computer_trash_file', {'path': 'Pictures/input.png'})
        answer = self.runtime.rpc({'capability': broker['capability'], 'op': 'approval', 'request': request})
        self.assertEqual(answer['state'], 'pending')
        self.assertIsNone(answer['grant'])
        self.runtime.approvals.decide('owner', 'private', 'original', 'assistant', answer['approval_id'], 'user-confirm', True)
        approved = self.runtime.rpc({'capability': broker['capability'], 'op': 'approval', 'request': request})
        changed = {**request, 'arguments': {**request['arguments'], 'path': 'Pictures/other.png'}}
        with self.assertRaises(AgentError):
            self.runtime.rpc({'capability': broker['capability'], 'op': 'consume', 'request': changed, 'grant': approved['grant']})
        self.assertEqual((self.root / 'Pictures/input.png').read_bytes(), PNG)

    def test_computer_scope_does_not_enable_legacy_commands_or_other_tools(self):
        with self.assertRaises(AgentError):
            self.runtime.submit(self.subject, 'legacy', 'run_command', {'program': '/bin/echo', 'argv': '["x"]'})
        self.runtime.computer_root = None
        with self.assertRaises(AgentError):
            self.runtime.submit(self.subject, 'new', 'computer_open_project', {'project': 'demo'})

    def test_alias_inspection_and_preflight_errors_are_not_executor_unknown(self):
        self.assertEqual(self.runtime.inspect_computer('folder', '~/Pictures')['path'], str(self.root / 'Pictures'))
        for value, code in [('~/missing-folder', 'COMPUTER_TARGET_NOT_FOUND'),
                            ('~other/Pictures', 'COMPUTER_TARGET_INVALID'),
                            ('~/.ssh', 'COMPUTER_TARGET_INVALID')]:
            with self.subTest(path=value), self.assertRaises(AgentError) as error:
                self.runtime.inspect_computer('folder', value)
            self.assertEqual(error.exception.code, code)
        with self.assertRaises(AgentError) as error:
            self.runtime.submit(self.subject, 'missing-image', 'computer_edit_image',
                dict(source='~/Pictures/missing.png', destination='~/Pictures/out.png', operation='rotate', value='90'))
        self.assertEqual(error.exception.code, 'COMPUTER_TARGET_NOT_FOUND')
        with mock.patch('agent_tasks.computer_inspect', side_effect=OSError('synthetic unexpected I/O')):
            with self.assertRaises(AgentError) as error:
                self.runtime.inspect_computer('folder', '~/Pictures')
            self.assertEqual(error.exception.code, 'EXECUTOR_UNKNOWN')
        with self.runtime.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)


class MobileConfirmation(unittest.TestCase):
    setUp = chat_tests.PluginAgentTests.setUp

    def trash_approval_event(self, paths):
        frozen = json.dumps({'source': {'stat': [1, 2, 29]}})
        return dict(task='internal-task', project='assistant', conversation='internal-conversation', kind='approval',
                    body=dict(tool='computer_trash_files', approval_id='a'*32, digest='internal-digest',
                              state='pending', arguments=dict(paths=json.dumps(paths), binding=json.dumps({
                                  'items': [{'path': path, 'binding': frozen} for path in paths]}))))

    def test_trash_approval_default_is_exact_file_scope_and_a_short_decision(self):
        event = self.trash_approval_event(['~/Pictures/demo/a.txt', '~/Pictures/demo/b.txt'])
        original = json.dumps(event)
        self.assertEqual(self.main._task_event_text(event),
                         '把这 2 个文件移入废纸篓？\n目录：~/Pictures/demo\n· a.txt\n· b.txt\n回复「同意」或「拒绝」。')
        detail = self.main._task_event_text(event, details=True)
        for field in ('~/Pictures/demo/a.txt', '~/Pictures/demo/b.txt', '58 字节', '四分钟',
                      '/agent approve ' + 'a'*32, '/agent deny ' + 'a'*32):
            self.assertIn(field, detail)
        self.assertEqual(json.dumps(event), original)

    def test_paths_in_different_directories_or_with_controls_stay_unambiguous(self):
        event = self.trash_approval_event(['folder-one/same.txt', 'folder-two/same.txt', 'folder-one/a\n\u202eb.txt'])
        compact = self.main._task_event_text(event)
        self.assertIn('· folder-one/same.txt\n· folder-two/same.txt', compact)
        self.assertIn(r"folder-one/a\n\u202eb.txt", compact)
        self.assertNotIn('\u202e', compact)
        self.assertNotIn('目录：', compact)

    def test_explicit_approval_lookup_shows_commands_but_cannot_approve(self):
        event = self.trash_approval_event(['~/Pictures/demo/a.txt', '~/Pictures/demo/b.txt'])
        approval = {**event['body'], 'ok': True, 'subject': dict(attempt=event['task'], project=event['project'],
                                                               conversation=event['conversation'])}
        self.main._control = mock.AsyncMock(return_value=(200, approval))
        self.main._request = mock.AsyncMock()
        self.event.get_message_str = lambda: '/agent approvals ' + approval['approval_id']
        asyncio.run(self.main.agent_message(self.event))
        self.assertEqual(self.main._control.await_count, 1)
        self.assertEqual(self.main._control.await_args.args[1],
                         {'op': 'agent.approvals', 'approval_id': approval['approval_id']})
        self.main._request.assert_not_awaited()
        self.assertIn('/agent approve ' + approval['approval_id'], self.main._send_notification.await_args.args[1])

    def test_trash_success_is_one_sentence_but_details_show_recovery(self):
        result = dict(evidence='batch_moved_to_trash_content_verified', count=2, staged_for_system_trash=True,
                      files=[dict(path='/synthetic/a'), dict(path='/synthetic/b')])
        event = dict(task='synthetic-task', conversation='original', project='assistant', kind='terminal',
                     body=dict(state='verified', result=result))
        self.assertEqual(self.main._task_event_text(event), '这 2 个文件已移入废纸篓。')
        detail = self.main._task_event_text(event, details=True)
        self.assertIn('/synthetic/a', detail)
        self.assertIn('/synthetic/b', detail)
        self.assertIn('手动移回原位置', detail)
        event['body']['state'] = 'process-ended'
        self.assertNotIn('已移入废纸篓', self.main._task_event_text(event))

    def test_reconciled_no_move_is_not_described_as_unknown(self):
        event = dict(task='synthetic-task', conversation='original', project='assistant', kind='snapshot',
                     body=dict(state='process-ended', result=dict(evidence='trash_no_move_host_reconciled', exit_code=1)))
        self.assertEqual(self.main._task_event_text(event), '已核对：那次操作没有移动文件，也没有自动重试。')

    def test_trash_partial_progress_must_match_the_exact_approved_order(self):
        paths = ['one.txt', 'two.txt', 'three.txt']
        args = json.dumps({'paths': json.dumps(paths)})
        result = dict(completion='uncertain', evidence='trash_stopped_with_progress', moved=paths[:1],
                      needs_check=paths[1], not_attempted=paths[2:], permanently_deleted=False)
        self.assertTrue(bound_trash_progress(result, 'computer_trash_files', args))
        self.assertFalse(bound_trash_progress(result, 'create_text', args))
        for replacement in (dict(moved=['other.txt']), dict(needs_check=paths[0]), dict(not_attempted=[]),
                            dict(not_attempted='three.txt'), dict(permanently_deleted=True), dict(extra='private')):
            self.assertFalse(bound_trash_progress({**result, **replacement}, 'computer_trash_files', args))
        event = dict(task='synthetic-task', conversation='original', project='assistant', kind='terminal',
                     body=dict(state='uncertain', result=result))
        text = self.main._task_event_text(event)
        self.assertIn('已核对移入废纸篓：\n· one.txt', text)
        self.assertIn('需要核查：two.txt', text)
        self.assertIn('本次未尝试：\n· three.txt', text)
        self.assertIn('不会自动重试', text)

    def test_pycharm_popup_reason_does_not_claim_project_process_completed(self):
        event = dict(task='synthetic-task', kind='terminal', conversation='synthetic-conversation',
            project='assistant', body=dict(state='process-ended',
            result=dict(evidence='pycharm_run_not_confirmed', ui_code='UI_BLOCKED')))
        text = self.main._task_event_text(event)
        self.assertIn('弹窗阻挡', text)
        self.assertIn('未点击运行', text)
        self.assertIn('没有自动重试', text)
        self.assertNotIn('进程已结束', text)

    def prompt(self, state='api_returned', count=1):
        expires = time.time() + 120
        subject = {'sender': 'owner', 'channel': self.event.unified_msg_origin,
                   'conversation': 'original', 'project': 'assistant', 'attempt': 'task-one'}
        approval = dict(approval_id='a' * 32, state='pending', subject=subject, expires=expires,
                        tool='computer_trash_file', digest='fixed-digest', arguments={'path': 'Pictures/unused.png', 'binding': 'frozen'})
        event = dict(id='event-one', task='task-one', kind='approval', conversation='original', project='assistant',
                     sender='owner', channel=self.event.unified_msg_origin, body={**approval, 'tool': 'computer_trash_file'})
        with self.main._agent_db() as db:
            db.execute('INSERT INTO agent_task_outbox VALUES (?,?,?,?,?)', ('event-one', 'owner',
                       self.event.unified_msg_origin, json.dumps(event), state))
            db.execute("INSERT INTO agent_confirmation_context VALUES (?,?,0,'event-one','')", ('owner', self.event.unified_msg_origin))
        self.main._control = mock.AsyncMock(side_effect=[(200, {'ok': True, 'approvals': [approval] * count}),
                                                        (200, {'ok': True, 'state': 'approved'})])
        self.main._request = mock.AsyncMock()
        self.event.get_message_str = lambda: '同意'
        self.stack.enter_context(mock.patch.object(self.plugin, 'AGENT_TASKS', True))
        return approval

    def test_plain_mobile_consent_uses_one_current_shown_approval_not_model(self):
        approval = self.prompt()
        asyncio.run(self.main.agent_message(self.event))
        payload = self.main._control.await_args.args[1]
        self.assertEqual(payload['op'], 'agent.approve')
        self.assertEqual(payload['approval_id'], approval['approval_id'])
        self.main._request.assert_not_awaited()
        self.assertEqual(self.main._send_notification.await_args.args[1], '收到，等执行结果。')

    def test_ambiguous_unshown_or_unknown_delivery_never_grants(self):
        self.prompt(state='unknown')
        asyncio.run(self.main.agent_message(self.event))
        self.assertEqual(self.main._control.await_count, 1)
        self.main._request.assert_not_awaited()
        self.assertIn('没有执行', self.main._send_notification.await_args.args[1])

    def test_multiple_pending_requests_do_not_guess_consent_target(self):
        self.prompt(count=2)
        asyncio.run(self.main.agent_message(self.event))
        self.assertEqual(self.main._control.await_count, 1)
        self.main._request.assert_not_awaited()
        self.assertIn('多项', self.main._send_notification.await_args.args[1])
        self.assertIn('/agent approvals', self.main._send_notification.await_args.args[1])

    def test_changed_or_foreign_prompt_cannot_be_approved(self):
        self.prompt()
        with self.main._agent_db() as db:
            row = db.execute('SELECT payload FROM agent_task_outbox').fetchone()
            value = json.loads(row[0])
            value['conversation'] = 'other-conversation'
            db.execute('UPDATE agent_task_outbox SET payload=?', (json.dumps(value),))
        asyncio.run(self.main.agent_message(self.event))
        self.assertEqual(self.main._control.await_count, 1)
        self.main._request.assert_not_awaited()

    def test_intervening_conversation_invalidates_bare_consent(self):
        self.prompt()
        self.main._advance_confirmation('owner', self.event.unified_msg_origin)
        asyncio.run(self.main.agent_message(self.event))
        self.assertEqual(self.main._control.await_count, 1)
        self.main._request.assert_not_awaited()
        self.assertIn('没有执行', self.main._send_notification.await_args.args[1])


class ComputerTransport(unittest.TestCase):
    def test_native_rpc_preserves_fixed_target_errors_not_untrusted_error_text(self):
        from assistant_executor import Executor
        node = os.environ.get('AGENTBRIDGE_TEST_NATIVE_NODE')
        if not node:
            self.skipTest('explicit native test runtime required')
        with tempfile.TemporaryDirectory(prefix='computer-transport-') as scratch:
            executor = Executor(Path(scratch).resolve(), 'http://127.0.0.1:12345', 'synthetic-secret',
                                types.SimpleNamespace(target=lambda _: 'unavailable'), computer_enabled=True)
            self.addCleanup(executor.close)
            subject = dict(sender='owner', channel='private', project='assistant', conversation='original', attempt='turn')
            broker = executor.issue('chat', subject)
            script = '''
import {rpc} from './agent_rpc.mjs';
try {await rpc(JSON.parse(process.argv[1]),'computer_inspect',{operation:'folder',value:'~/missing'});
  process.stdout.write('unexpected success');}
catch(error){process.stdout.write(error.message);}
'''
            for code in ('COMPUTER_TARGET_NOT_FOUND', 'COMPUTER_TARGET_DENIED', 'COMPUTER_TARGET_INVALID', 'UNTRUSTED_ERROR_TEXT'):
                executor.call = mock.Mock(return_value={'ok': False, 'code': code})
                result = subprocess.run([node, '--input-type=module', '-e', script, json.dumps(broker)],
                    cwd=Path(__file__).parent, capture_output=True, text=True,
                    env={**os.environ, 'ELECTRON_RUN_AS_NODE': '1'}, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, code if code.startswith('COMPUTER_TARGET_') else 'TASK_RPC_DENIED')
                self.assertEqual(executor.call.call_args.args[2]['value'], '~/missing')

    def test_explicit_computer_capability_does_not_enable_old_write_interface(self):
        from assistant_executor import Executor
        with tempfile.TemporaryDirectory(prefix='computer-transport-') as scratch:
            root = Path(scratch).resolve()
            projects = types.SimpleNamespace(target=lambda _: 'unavailable')
            executor = Executor(root, 'http://127.0.0.1:12345', 'synthetic-secret', projects, computer_enabled=True)
            self.addCleanup(executor.close)
            subject = {'sender': 'owner', 'channel': 'private', 'project': 'assistant', 'conversation': 'original', 'attempt': 'turn'}
            broker = executor.issue('chat', subject)
            executor.call = mock.Mock(return_value={'ok': True, 'task_id': 'synthetic-only'})
            payload = {'capability': broker['capability'], 'op': 'submit', 'call_id': 'call-one',
                       'tool': 'computer_open_project', 'arguments': {'project': 'demo'}}
            self.assertEqual(executor.rpc(payload)['task_id'], 'synthetic-only')
            self.assertEqual(executor.call.call_args.args[0], subject)
            with self.assertRaises(AgentError):
                executor.rpc({**payload, 'tool': 'run_command', 'arguments': {'program': '/bin/echo', 'argv': '[]'}})
            executor.computer_enabled = False
            with self.assertRaises(AgentError):
                executor.rpc(payload)

    def test_mac_dispatch_uses_host_file_scope_not_model_project_path(self):
        import executor_api
        tasks = types.SimpleNamespace(workspace=mock.Mock(return_value='/synthetic/host-root'), submit=mock.Mock(return_value={'ok': True}))
        app = types.SimpleNamespace(role='executor_only', agent_tasks=tasks, boot_id='boot',
                                   get_project=mock.Mock(side_effect=AssertionError('computer tasks should not require project switching')))
        subject = {'sender': 'owner', 'channel': 'private', 'project': 'assistant', 'conversation': 'original', 'attempt': 'turn'}
        payload = {'sender': 'owner', 'channel': 'private', 'op': 'submit', 'subject': subject,
                   'call_id': 'one', 'tool': 'computer_open_project', 'arguments': {'project': 'demo'}}
        self.assertTrue(executor_api.dispatch(app, payload, {'owner'}, {'private'})['ok'])
        self.assertEqual(tasks.submit.call_args.args[0]['cwd'], '/synthetic/host-root')
        with self.assertRaises(AgentError):
            executor_api.dispatch(app, {**payload, 'sender': 'other'}, {'owner'}, {'private'})
