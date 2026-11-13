"""Mac-private run grants. Called by authenticated user controls, never tools."""
import json
import time
import uuid

from agent_protocol import AgentError
from agent_approvals import canonical, fingerprint


WARNING = '以后此配置运行的项目代码继承当前Mac账号权限，可能写文件或访问网络，不是沙箱；代码日常修改不撤销授权。'


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS run_permissions('
               'id TEXT PRIMARY KEY,sender TEXT,channel TEXT,binding TEXT,digest TEXT,state TEXT,created REAL)')


def binding(arguments):
    value = json.loads(arguments['binding'])['run']
    if not isinstance(value, dict) or not value.get('actual_interpreter'):
        raise AgentError('APPROVAL_DENIED', '无法核对运行配置或解释器，不授予长期权限')
    return value


def create(db, subject, arguments):
    value = binding(arguments)
    digest = fingerprint(value)
    old = db.execute("SELECT id FROM run_permissions WHERE sender=? AND channel=? AND digest=? AND state='active'",
                     (subject['sender'], subject['channel'], digest)).fetchone()
    if old:
        return old['id']
    identifier = uuid.uuid4().hex
    db.execute('INSERT INTO run_permissions VALUES (?,?,?,?,?,?,?)',
               (identifier, subject['sender'], subject['channel'], canonical(value), digest, 'active', time.time()))
    return identifier


def matching(db, subject, arguments):
    value = binding(arguments)
    digest = fingerprint(value)
    rows = db.execute("SELECT * FROM run_permissions WHERE sender=? AND channel=? AND state='active'",
                      (subject['sender'], subject['channel'])).fetchall()
    matched = None
    for row in rows:
        old = json.loads(row['binding'])
        if (old['project_path'], old['configuration']) != (value['project_path'], value['configuration']):
            continue
        if row['digest'] == digest:
            matched = row['id']
        else:
            db.execute("UPDATE run_permissions SET state='invalidated' WHERE id=?", (row['id'],))
    return matched


def listing(db, sender, channel):
    rows = db.execute("SELECT * FROM run_permissions WHERE sender=? AND channel=? AND state='active' ORDER BY created",
                      (sender, channel)).fetchall()
    visible = ('project_path', 'configuration', 'entry', 'working_directory', 'actual_interpreter')
    return {'ok': True, 'permissions': [dict(permission_id=row['id'], **{
                key: value for key, value in json.loads(row['binding']).items() if key in visible}) for row in rows],
            'warning': WARNING}


def revoke(db, sender, channel, identifier):
    row = db.execute('SELECT * FROM run_permissions WHERE id=? AND sender=? AND channel=?',
                     (identifier, sender, channel)).fetchone()
    if row is None:
        raise AgentError('NOT_FOUND', '授权不存在或不属于当前来源')
    db.execute("UPDATE run_permissions SET state='revoked' WHERE id=?", (identifier,))
    return {'ok': True, 'permission_id': identifier, 'state': 'revoked'}
