"""Reviewed test-only row repair. Default is read-only; never restores a database."""
import ast
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path.home() / 'agentbridge'
EVIDENCE = Path.home() / '.local/share/agentbridge-releases/20261026-stability-experience'
TABLES = ('jobs', 'projects', 'channels', 'pending', 'aliases', 'quarantine', 'control_receipts')
EXPECTED = {'jobs': (3, 1), 'projects': (0, 10), 'channels': (0, 3)}


def connect(path):
    db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def rows(db, table, columns, keys):
    names = ','.join('"' + name + '"' for name in columns)
    return {tuple(r[k] for k in keys): dict(r) for r in db.execute('SELECT ' + names + ' FROM ' + table)}


def repair(apply=False):
    os.umask(0o077)
    report = json.loads((EVIDENCE / 'discovery-impact-private.json').read_text())
    literals = {node.value for node in ast.walk(ast.parse((ROOT / 'test_worker.py').read_text()))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    with closing(connect(EVIDENCE / 'pre-maintenance-worker.sqlite3')) as before, \
            closing(connect(EVIDENCE / 'post-discovery-worker.sqlite3')) as after:
        changes, added_ids = [], []
        for table in TABLES:
            columns = [r[1] for r in before.execute('PRAGMA table_info(' + table + ')')]
            keys = [r[1] for r in before.execute('PRAGMA table_info(' + table + ')') if r[5]]
            old, new = rows(before, table, columns, keys), rows(after, table, columns, keys)
            assert not old.keys() - new.keys(), 'Unexpected removed record; manual review required'
            added = [new[k] for k in new.keys() - old.keys()]
            changed = [(old[k], new[k]) for k in old.keys() & new.keys() if old[k] != new[k]]
            assert (len(added), len(changed)) == EXPECTED.get(table, (0, 0)), 'Diff scope changed'
            for row in added:
                assert table == 'jobs' and not row['sender'] and row['task'] in literals
                assert row['channel'] in literals and row['status'] == 'done'
                assert any(x['job_id'] == row['job_id'] for x in report['jobs']['added'])
                added_ids.append(row['job_id'])
                changes.append((table, keys, None, row, columns))
            for old_row, new_row in changed:
                changed_columns = [c for c in columns if old_row[c] != new_row[c]]
                if table == 'jobs':
                    assert set(changed_columns) == {'status', 'error_code'}
                    assert old_row['job_id'] in literals and not old_row['sender']
                elif table == 'projects':
                    assert set(changed_columns) == {'dir', 'normalized', 'lock_key'}
                    assert old_row['name'] in literals and old_row['name'] != 'default'
                    assert '/agentbridge-test-' in new_row['dir']
                else:
                    assert table == 'channels' and changed_columns == ['updated_at']
                changes.append((table, keys, old_row, new_row, changed_columns))
        with closing(sqlite3.connect(ROOT / 'agentbridge.db', timeout=10)) as live:
            live.row_factory = sqlite3.Row
            if apply:
                backup = EVIDENCE / ('pre-repair-' + str(time.time_ns()) + '.sqlite3')
                with closing(sqlite3.connect(backup)) as target:
                    live.backup(target)
            live.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
            try:
                assert live.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] == 0
                for table, keys, old_row, expected, columns in changes:
                    where = ' AND '.join('"' + key + '"=?' for key in keys)
                    row = live.execute('SELECT * FROM ' + table + ' WHERE ' + where,
                                       tuple(expected[k] for k in keys)).fetchone()
                    assert row is not None and all(row[k] == v for k, v in expected.items()), 'Concurrent row change; abort'
                for job_id in added_ids:
                    for table in ('attempts', 'completion_events', 'aliases', 'pending', 'control_receipts'):
                        current = [tuple(r) for r in live.execute('SELECT * FROM ' + table + ' WHERE job_id=? ORDER BY 1', (job_id,))]
                        saved = [tuple(r) for r in after.execute('SELECT * FROM ' + table + ' WHERE job_id=? ORDER BY 1', (job_id,))]
                        assert current == saved, 'New authorization or execution metadata; abort'
                if apply:
                    for table, keys, old_row, expected, columns in changes:
                        where = ' AND '.join('"' + key + '"=?' for key in keys)
                        values = tuple(expected[k] for k in keys)
                        if old_row is None:
                            live.execute('DELETE FROM completion_events WHERE job_id=?', (expected['job_id'],))
                            live.execute('DELETE FROM attempts WHERE job_id=?', (expected['job_id'],))
                            live.execute('DELETE FROM jobs WHERE ' + where, values)
                        else:
                            assignments = ','.join('"' + col + '"=?' for col in columns)
                            live.execute('UPDATE ' + table + ' SET ' + assignments + ' WHERE ' + where,
                                         tuple(old_row[col] for col in columns) + values)
                    live.commit()
                else:
                    live.rollback()
            except BaseException:
                live.rollback()
                raise
    result = {'dry_run': not apply, 'test_only_new_jobs': 3, 'job_fields_restored': 1,
              'project_bindings_restored': 10, 'channel_timestamps_restored': 3,
              'full_database_restore': False, 'audit_logs_deleted': False}
    if apply:
        result['backup_sha256'] = hashlib.sha256(backup.read_bytes()).hexdigest()
        (EVIDENCE / 'incident-repair-result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    assert sys.argv[1:] in ([], ['--apply-reviewed-test-only-diff'])
    repair(bool(sys.argv[1:]))
