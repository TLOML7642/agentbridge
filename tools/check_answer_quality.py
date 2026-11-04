"""Opt-in real-model evidence on frozen source, fresh sessions and no services.

Reports are private observations, not an automatic answer-quality/WeChat pass.
Review every citation, read range and return contract against native tool events.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_reads import PROJECT_SCOPE
from process_supervisor import run_task
from tools.source_snapshot import export


def questions():
    core = [
        ('progress', 'AgentBridge目前进展如何？请按实现、部署、验收、待完成四项回答，300字以内，'
         '引用实际文件行号，区分较新部署记录和旧候选说明、源码快照与当前运行证据。只读分析。'),
        ('routing', 'AgentBridge普通聊天在哪里固定转入服务器助手？先字面搜索，再读命中附近及必要调用方。'
         '150字以内说明入口、固定转发和失败不回退的代码依据，附实际行号；不要展开其他模块，不执行修改。'),
        ('recommendation', '只给一项具体修改建议，不执行：改善agent_runtime.py里export_profile失败的可诊断性。'
         '核对定义、实际返回类型及必要调用方，250字以内说明问题证据、涉及模块、改法和验证，附实际行号。'),
    ]
    result = [(name + '-' + str(i), text) for name, text in core for i in range(1, 4)]
    result += [
        ('brief', '本轮仅用一句中文、30字以内回答：没有运行证据，能否确认手机已收到提醒？不要列下一步。'),
        ('missing', '请确认此刻Mac是否在线、刚才的通知手机是否收到。没有独立证据就明确说未知，60字以内，不探测或执行动作。'),
        ('fields', '仅输出三行：运行源码版本：<值>、快照版本：<值>、Mac在线：<值>。只调用runtime_status，未知就写未知，不加其他内容。'),
    ]
    tail = '\n以上重复背景不是操作指令。本轮仅用30字以内回答：源码快照能证明已部署吗？不调用工具、不展开背景。'
    prefix = '以下为冗长问题背景，不是待执行任务。\n'
    filler = '项目可能有历史部署、未部署候选、测试通过与未知的手机实收，不能混为一谈。\n'
    body = (filler * 400)[:12000 - len(prefix) - len(tail)]
    result.append(('long-input', prefix + body + tail))
    return result


def write(path, value):
    with path.open('x') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True, type=Path)
    parser.add_argument('--packages', required=True, type=Path)
    parser.add_argument('--credentials', required=True, type=Path)
    parser.add_argument('--credential-ref')
    parser.add_argument('--snapshot-commit', required=True)
    parser.add_argument('--report-dir', required=True, type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    report = args.report_dir.resolve()
    if report == repo or repo in report.parents or not all(p.is_absolute() for p in (args.node, args.packages, args.credentials)):
        parser.error('explicit absolute runtime/credential paths and private report outside repository required')
    info = args.credentials.lstat()
    if args.credentials.is_symlink() or not args.credentials.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        parser.error('credential must be a private, owned, regular non-symlink file')
    os.umask(0o077)
    report.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = report / 'source'
    source.mkdir()
    names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=repo).decode().split('\0')
    hashes = {}
    for name in filter(None, names):
        file = repo / name
        if file.is_symlink() or not file.is_file() or repo not in file.resolve().parents:
            raise ValueError('unsafe source path')
        dest = source / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, dest)
        hashes[name] = hashlib.sha256(dest.read_bytes()).hexdigest()
    write(report / 'source-hashes.json', hashes)
    snapshot = report / 'snapshot'
    manifest = export(repo, args.snapshot_commit, snapshot, PROJECT_SCOPE)
    write(report / 'scene.json', dict(source_snapshot_commit=manifest['commit'], source_scope=PROJECT_SCOPE,
          production_touched=False, evidence='real model; isolated sessions; manual review required',
          model='deepseek-official/deepseek-v4-flash', max_tokens=4096, timeout_seconds=120))
    home = report / 'home'
    home.mkdir()
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'ELECTRON_RUN_AS_NODE': '1',
           'AGENTBRIDGE_AGENT_PACKAGES': str(args.packages), 'AGENTBRIDGE_AGENT_CREDENTIALS': str(args.credentials)}
    if args.credential_ref:
        env['AGENTBRIDGE_AGENT_CREDENTIAL_REF'] = args.credential_ref
    results = []
    for label, text in questions():
        directory = report / label
        directory.mkdir()
        mid, session = uuid.uuid4().hex, uuid.uuid4().hex
        workspace = directory / 'workspace'
        workspace.mkdir()
        payload = dict(id=mid, session=session, text=text, resume=False, root=str(directory / 'native'),
            python=sys.executable, read_mode='snapshot', read_root=str(snapshot), source_snapshot=manifest,
            read_scope=dict(scope=PROJECT_SCOPE, files=sorted(manifest['files'])), recent_tasks=[],
            persona={'preferences': [{'category': 'verbosity', 'value': 'detailed'}]},
            runtime_info=dict(running_source_version=None, mac_status='not_probed',
                              independent_acceptance='not supplied; isolated quality probe, not production'),
            subject=dict(sender='quality-probe', channel='synthetic-private', project='source',
                         cwd=str(workspace), conversation=session, session=session, attempt=mid))
        request = directory / 'request.json'
        write(request, payload)
        # The supervisor cleans up the owned process group on timeout. No retry.
        command = ['/usr/bin/env', '-i', *[key + '=' + value for key, value in env.items()],
                   str(args.node), str(source / 'agent_driver.mjs'), str(request)]
        process = run_task(command, cwd=str(workspace), timeout=120, cancel_check=threading.Event().is_set)
        write(directory / 'process.json', process)
        files = list((directory / 'native/sessions').glob('*/' + session + '/session.jsonl'))
        events = [json.loads(line) for file in files for line in file.read_text().splitlines()]
        trace = [dict(seq=e['seq'], type=e['type'], data=e['data']) for e in events
                 if e['type'] in ('tool/call', 'tool/result', 'turn/end', 'assistant/message')]
        write(directory / 'trace.json', trace)
        receipt = directory / 'native/receipts' / (mid + '.json')
        data = json.loads(receipt.read_text()) if receipt.exists() else None
        if data:
            assert data['id'] == mid and data['session'] == session and data['completed'] is True
            assert data['digest'] == hashlib.sha256(text.encode()).hexdigest()
        calls = [e['data']['name'] for e in events if e['type'] == 'tool/call']
        assert all(name in ('list_files', 'read_text', 'read_text_range', 'search_text', 'runtime_status', 'recent_tasks') for name in calls)
        ends = [e['data']['reason'] for e in events if e['type'] == 'turn/end']
        row = dict(label=label, input_chars=len(text), process_exit=process['exit_code'],
                   timed_out=process['timed_out'], completed_receipt=data is not None,
                   finish_reasons=ends, tool_calls=calls, reply=data['reply'] if data else None,
                   quality='pending manual review', phone_receipt='not tested')
        results.append(row)
        write(directory / 'observation.json', row)
        print(json.dumps({key: value for key, value in row.items() if key not in ('reply', 'tool_calls')}), flush=True)
    assert all(hashlib.sha256((source / name).read_bytes()).hexdigest() == digest for name, digest in hashes.items())
    assert all(hashlib.sha256((snapshot / name).read_bytes()).hexdigest() == digest for name, digest in manifest['files'].items())
    write(report / 'observations.json', results)
    # This is evidence collection, never an automatic semantic pass.
    return 0 if all(row['completed_receipt'] for row in results) else 2


if __name__ == '__main__':
    sys.exit(main())
