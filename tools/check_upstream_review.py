"""Validate private, whole-group manual reviews without scoring model answers.

The manifest binds each review to frozen questions, observations and (where
needed) the separately saved native/product normalization. Nothing is sent to
a model, and the result contains counts and hashes, never answer text.
"""
import argparse
import hashlib
import json
from pathlib import Path


AXES = ('factual_correctness', 'evidence_reliability',
        'task_completeness', 'expression_compliance')
GROUPS = {'astrbot-workspace-r3', 'openclaw-local-r1', 'dsh-old-cli-r2',
          'dsh-new-sdk-r2', 'f02-r1'}
STATUSES = {'passed', 'failed', 'insufficient'}
CASES_SHA256 = 'a5c55a3d9491bcf274c21c831867247d6eccf9acd453c5aef8c3efd48bde00d2'
NORMALIZED_GROUPS = {'astrbot-workspace-r3', 'openclaw-local-r1', 'dsh-old-cli-r2'}
OBSERVATIONS_SHA256 = {
    'astrbot-workspace-r3': 'b9dceb05dfed73440546b9cc2ebe69ddad26a58b02dd30835eed236ab0f2dcfd',
    'openclaw-local-r1': 'b22f8a029a89923d6d330cb2fb7f578685f5f82c6486352391d14cdab8ab2e16',
    'dsh-old-cli-r2': '457a36a89ea3a2169125a050a0a80b72e067f3de2dd533e1f8ce5628452b8c4b',
    'dsh-new-sdk-r2': 'c22dca414453b83fbc3f0272175bb7c1ff83b1d64addd273c1f1366b08791e06',
    'f02-r1': 'b560f005960fa4235e5d27ce096bb93552849621433754b74c4df2d59cd92d36',
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def text_hash(value):
    # Null (no delivered answer) is distinct from an empty or whitespace reply.
    return sha256(json.dumps(value, ensure_ascii=False).encode())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_artifact(spec, root):
    path = Path(spec['path'])
    require(not path.is_absolute() and '..' not in path.parts, 'unsafe artifact path')
    target = root / path
    require(not target.is_symlink() and root in target.resolve().parents,
            'artifact escapes private evidence directory')
    data = target.read_bytes()
    require(sha256(data) == spec['sha256'], 'artifact hash mismatch')
    return json.loads(data)


def answers(row, normalized=None):
    if normalized is not None:
        return normalized['provider_last_response'], normalized.get(
            'dashboard_display', normalized.get('cli_display'))
    return row.get('native_reply', row.get('reply')), row.get('reply')


def check(manifest_path):
    manifest_path = Path(manifest_path).resolve(strict=True)
    root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    require(manifest['format'] == 1, 'unsupported manifest format')
    require(manifest['cases']['sha256'] == CASES_SHA256, 'not the frozen r2 question artifact')
    cases = read_artifact(manifest['cases'], root)
    require(len(cases) == 30 and len({r['id'] for r in cases}) == 30,
            'expected all 30 frozen questions')
    for case in cases:
        require(sha256(case['question'].encode()) == case['question_sha256'],
                'question hash mismatch')
    groups = manifest['groups']
    require(len(groups) == len(GROUPS) and {g['id'] for g in groups} == GROUPS,
            'expected five independent groups, without duplicates')
    summary = []
    for group in groups:
        require(group['observations']['sha256'] == OBSERVATIONS_SHA256[group['id']],
                'observations do not belong to this frozen group')
        require(('normalized' in group) == (group['id'] in NORMALIZED_GROUPS),
                'native/product normalization required for this group')
        rows = read_artifact(group['observations'], root)
        require([r['input'] for r in rows] == cases, 'frozen inputs/order/preferences differ')
        normalized = None
        if 'normalized' in group:
            artifact = read_artifact(group['normalized'], root)
            require(artifact['summary']['source_sha256'] == group['observations']['sha256'],
                    'normalization belongs to another group')
            normalized = artifact['cases']
            require([r['id'] for r in normalized] == [c['id'] for c in cases],
                    'normalized cases missing or reordered')
        reviews = read_artifact(group['reviews'], root)
        require([r['id'] for r in reviews] == [c['id'] for c in cases],
                'manual reviews missing or reordered')
        for index, (case, row, review) in enumerate(zip(cases, rows, reviews)):
            require(row['id'] == case['id'], 'observation ID mismatch')
            require(type(row['model_turn_completed']) is bool, 'invalid completion flag')
            native, display = answers(row, normalized[index] if normalized else None)
            if normalized:
                require(type(normalized[index]['model_turn_completed']) is bool
                        and normalized[index]['model_turn_completed'] == row['model_turn_completed'],
                        'normalization changed completion flag')
            require(review['question_sha256'] == case['question_sha256'], 'review question mismatch')
            require(review['native_sha256'] == text_hash(native), 'review native answer mismatch')
            require(review['display_sha256'] == text_hash(display), 'review product answer mismatch')
            require(type(review['model_turn_completed']) is bool
                    and review['model_turn_completed'] == row['model_turn_completed'],
                    'review changed completion flag')
            require(review['wechat_display'] == 'not_tested'
                    and review['phone_receipt'] == 'not_tested'
                    and review['user_goal_completion'] == 'not_verified',
                    'offline review cannot establish workflow/device acceptance')
            require(review['protocol_gap'] in ('runtime_status', None), 'unknown protocol gap')
            for layer in ('native', 'product'):
                axes = review[layer]
                require(set(axes) == set(AXES), 'four axes required for each answer layer')
                for axis, result in axes.items():
                    require(result['status'] in STATUSES, 'pending or invalid axis status')
                    require(isinstance(result['reason'], str) and result['reason'].strip(),
                            'manual axis explanation required')
                    if not row['model_turn_completed']:
                        require(result['status'] != 'passed', 'unfinished answer cannot pass')
                if review['protocol_gap']:
                    require(axes['task_completeness']['status'] != 'passed',
                            'missing required private protocol cannot pass completeness')
        summary.append({'group': group['id'], 'reviewed': len(reviews),
                        'model_turns_completed': sum(r['model_turn_completed'] for r in rows),
                        'observations_sha256': group['observations']['sha256'],
                        'reviews_sha256': group['reviews']['sha256']})
    return {'groups': summary, 'reviewed': 150, 'real_model_calls': 0,
            'deployment': 'not_performed', 'acceptance': 'not_established',
            'interpretation': 'manual review coverage, not correctness rate or release approval'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.manifest), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
