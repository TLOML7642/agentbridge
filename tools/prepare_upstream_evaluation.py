"""Freeze the existing 30 cases and public source for upstream comparisons.

This prepares private evidence, not a runner, adapter, quality score or release
approval. Candidate entrypoints must remain upstream-owned and run separately.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_reads import PROJECT_SCOPE
from tools.check_answer_quality import (
    REVIEW_CRITERIA, boundary_questions, natural_questions, questions,
)
from tools.source_snapshot import export

SNAPSHOT = '6da67bfe21592ab3823a1eca9058ce1d27266a38'
BASELINE = 'f02fd75821c9b23ec43b9ddcf8a77928c378118b'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def frozen_cases():
    rows = []
    for suite, items in (('core', questions()), ('natural', natural_questions()),
                         ('boundary', boundary_questions())):
        for label, text in items:
            preferences = []
            if not label.endswith('-default'):
                preferences = [{'category': 'verbosity',
                                'value': 'brief' if label.endswith('-brief') else 'detailed'}]
            rows.append(dict(id=label, suite=suite, question=text,
                             question_sha256=digest(text.encode()),
                             preferences=preferences,
                             independent_session=True,
                             review={axis: 'pending' for axis in REVIEW_CRITERIA},
                             protocol_compatibility='pending',
                             user_goal_completion='unreviewed'))
    return rows


def write_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def prepare(report, repo=ROOT):
    report, repo = Path(report).absolute(), Path(repo).resolve(strict=True)
    if (report.is_symlink() or report == repo or repo in report.resolve().parents
            or report.exists()):
        raise ValueError('report must be a new private directory outside repository')
    # A missing commit fails before creating an apparently complete scene.
    for commit in (BASELINE, SNAPSHOT):
        subprocess.run(['git', 'cat-file', '-e', commit + '^{commit}'], cwd=repo, check=True)
    report.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = export(repo, SNAPSHOT, report / 'snapshot', PROJECT_SCOPE)
    baseline = report / 'baseline-f02fd75'
    baseline.mkdir(mode=0o700)
    entries = subprocess.check_output(['git', 'ls-tree', '-rz', '--full-tree', BASELINE], cwd=repo)
    hashes = {}
    for entry in filter(None, entries.split(b'\0')):
        metadata, raw_name = entry.split(b'\t', 1)
        mode, kind, oid = metadata.split()
        name = raw_name.decode('utf-8')
        relative = Path(name)
        if (mode not in (b'100644', b'100755') or kind != b'blob'
                or relative.is_absolute() or '..' in relative.parts):
            raise ValueError('baseline contains a non-regular or unsafe source path')
        content = subprocess.check_output(['git', 'cat-file', 'blob', oid], cwd=repo)
        dest = baseline / relative
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with dest.open('xb') as stream:
            stream.write(content)
        hashes[name] = digest(content)
    rows = frozen_cases()
    write_json(report / 'cases.json', rows)
    write_json(report / 'criteria.json', REVIEW_CRITERIA)
    write_json(report / 'baseline-hashes.json', hashes)
    scene = dict(source_snapshot=manifest, baseline_commit=BASELINE,
                 cases=len(rows), cases_sha256=digest((report / 'cases.json').read_bytes()),
                 api_base='https://api.deepseek.com', model='deepseek-v4-flash',
                 max_tokens=4096, reasoning_effort='high', timeout_seconds=120,
                 model_mapping='legacy ID; verify current official mapping before real requests',
                 production_data_included=False, candidate_runs=0,
                 adoption='not_decided', deployment='not_performed',
                 boundary_preferences='detailed, matching the existing boundary payload',
                 protocol_gap_policy='record separately; never replace or remove the question',
                 holdout='six unseen questions, three sessions each, after candidate selection')
    write_json(report / 'scene.json', scene)
    # The snapshots are inputs, not writable candidate workspaces.
    for tree in (baseline, report / 'snapshot'):
        for path in sorted(tree.rglob('*'), key=lambda p: len(p.parts), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        tree.chmod(0o555)
    return scene


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    scene = prepare(args.report_dir)
    print(json.dumps({key: scene[key] for key in ('cases', 'cases_sha256', 'baseline_commit',
                                                 'candidate_runs', 'adoption', 'deployment')}))


if __name__ == '__main__':
    main()
