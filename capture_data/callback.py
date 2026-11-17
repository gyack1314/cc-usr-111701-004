"""接收 WebSub 回调：签名、topic、内容摘要、租约有效期一起核对。

回调只生成“值得重新抓取”的通知，通知不写入 articles/revisions 账本，
因此重复投递不会变成新稿，漏掉的通知也不会因晚到稿件被掩盖。
"""
import base64
import hashlib
import json
from datetime import datetime, timezone

from . import signature
from .lease import iso, lease_context, utc

SIGNATURE_HEADERS = ('x-hub-signature-256', 'x-hub-signature')
DIGEST_HEADERS = ('digest', 'content-digest', 'x-content-sha256')
MAX_BODY = 1 << 20


def _now(at):
    return iso(utc(at)) if at else datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _header(headers, names):
    for name in names:
        value = headers.get(name)
        if value:
            return value
    return None


def _digest_check(header, body):
    """核验可选的 Digest 头；未提供时由请求签名承担内容绑定。"""
    if header is None:
        return {'ok': True, 'detail': '未提供独立摘要头'}
    text = header.strip()
    candidate = None
    if '=' in text:
        algorithm, _, value = text.partition('=')
        algorithm = algorithm.strip().lower()
        value = value.strip()
        if algorithm in ('sha-256', 'sha256'):
            try:
                candidate = base64.b64decode(value, validate=True).hex()
            except Exception:
                candidate = value.lower() if len(value) == 64 else None
        else:
            return {'ok': False, 'detail': '不支持的摘要算法:' + algorithm}
    else:
        candidate = text.lower() if len(text) == 64 else None
    if candidate is None:
        return {'ok': False, 'detail': '摘要格式非法'}
    actual = hashlib.sha256(body).hexdigest()
    return {'ok': candidate == actual, 'detail': None if candidate == actual else '内容摘要与原文不符'}


def _entries(doc):
    raw = doc.get('entries')
    if not isinstance(raw, list):
        raise ValueError('entries 必须为数组')
    seen = set()
    entries = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError('通知条目必须为对象')
        article = item.get('id')
        revision = item.get('revision')
        source = item.get('source', '')
        deleted = item.get('deleted', False)
        cursor = item.get('cursor')
        if not isinstance(article, str) or not article:
            raise ValueError('通知条目缺少文章标识')
        if not isinstance(revision, str) or not revision:
            raise ValueError('通知条目缺少修订标识')
        if not isinstance(source, str) or (source and not source.startswith(('http://', 'https://'))):
            raise ValueError('通知条目来源地址非法')
        if not isinstance(deleted, bool):
            raise ValueError('通知条目撤稿标记非法')
        if cursor is not None and (type(cursor) is not int or cursor < 0):
            raise ValueError('通知条目游标非法')
        identity = (article, revision, source, deleted)
        if identity in seen:
            continue
        seen.add(identity)
        entries.append({'article_id': article, 'revision': revision, 'source_url': source,
                        'deleted': deleted, 'cursor': cursor, 'position': position})
    return entries


def _record(conn, topic, body_digest):
    return conn.execute(
        'SELECT id,accepted,last_accepted,deliveries,generation FROM callbacks '
        'WHERE topic=? AND body_digest=?', (topic, body_digest)).fetchone()


def receive(conn, topic, body, headers=None, at=None):
    """接收一次回调请求，返回核验结果与新增通知；同字节重送不生成第二份通知。"""
    if isinstance(body, str):
        body = body.encode('utf-8')
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_BODY:
        raise ValueError('回调请求体非法或过大')
    body = bytes(body)
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    now = _now(at)
    declared_topic = _subscription(conn, topic)[0]
    return _receive(conn, topic, declared_topic, body, headers, now)


def _subscription(conn, topic):
    subscription = conn.execute('SELECT topic,hub,secret FROM subscriptions WHERE topic=?', (topic,)).fetchone()
    if not subscription:
        raise ValueError('未知订阅')
    return subscription


def _receive(conn, topic, declared_topic, body, headers, now):
    digest_value = hashlib.sha256(body).hexdigest()
    previous = _record(conn, topic, digest_value)

    checks = {}
    reasons = []
    doc = None
    lease_id = None
    context = {'usable': False, 'generation': None, 'reason': '无法解析'}
    entries = []
    try:
        doc = json.loads(body.decode('utf-8'))
        if not isinstance(doc, dict):
            raise ValueError('回调不是 JSON 对象')
        checks['json'] = True
    except Exception as exc:
        checks['json'] = False
        reasons.append('请求体不是合法回调:' + str(exc))
        doc = None

    if doc is not None:
        digest_check = _digest_check(_header(headers, DIGEST_HEADERS), body)
        checks['content_digest'] = digest_check['ok']
        if not digest_check['ok']:
            reasons.append(digest_check['detail'])

        signed = signature.verify(_header(headers, SIGNATURE_HEADERS), body, _subscription(conn, topic)[2])
        checks['signature'] = signed['valid']
        if not signed['valid']:
            reasons.append(signed['reason'])

        body_topic = doc.get('topic')
        checks['topic'] = body_topic == declared_topic
        if not checks['topic']:
            reasons.append('回调 topic 与订阅不符')

        body_hub = doc.get('hub')
        lease_id = doc.get('lease_id')
        lease_expires = doc.get('lease_expires')
        if not body_hub or not lease_id or not lease_expires:
            checks['hub'] = False
            checks['lease'] = False
            checks['lease_expiry'] = False
            checks['entries'] = False
            reasons.append('回调缺少 hub 或租约字段')
        else:
            checks['hub'] = body_hub == _subscription(conn, topic)[1]
            if not checks['hub']:
                reasons.append('回调 hub 与订阅不符')
            try:
                lease_expiry_ok = utc(lease_expires) > utc(now)
                if not lease_expiry_ok:
                    reasons.append('回调声明租约已过期')
            except ValueError:
                lease_expiry_ok = False
                reasons.append('回调租约有效期格式非法')
            context = lease_context(conn, declared_topic, body_hub, lease_id, now,
                                    lease_expires if lease_expiry_ok else None)
            checks['lease_expiry'] = lease_expiry_ok
            checks['lease'] = context['usable']
            if not context['usable']:
                reasons.append(context['reason'])
            try:
                entries = _entries(doc)
                checks['entries'] = True
            except ValueError as exc:
                entries = []
                checks['entries'] = False
                reasons.append(str(exc))
    else:
        checks.update({'content_digest': False, 'signature': False, 'topic': False, 'hub': False,
                       'lease': False, 'lease_expiry': False, 'entries': False})

    accepted = not reasons
    generation = context['generation']
    checks_json = json.dumps(checks, ensure_ascii=False, sort_keys=True)
    reasons_json = json.dumps(reasons, ensure_ascii=False)
    active_generation = conn.execute('SELECT active_generation FROM subscriptions WHERE topic=?',
                                     (declared_topic,)).fetchone()[0]
    overlap = bool(accepted and generation is not None and generation != active_generation)
    recorded_hub = (doc or {}).get('hub') if isinstance(doc, dict) else None
    recorded_topic = (doc or {}).get('topic') if isinstance(doc, dict) else None
    duplicate = previous is not None and bool(previous[2])
    inserted = []

    with conn:
        if previous:
            conn.execute(
                'UPDATE callbacks SET deliveries=deliveries+1,last_seen_at=?,last_accepted=?,checks=?,'
                'reasons=?,overlap=?,generation=CASE WHEN ? THEN ? ELSE generation END WHERE id=?',
                (now, int(accepted), checks_json, reasons_json, int(overlap), int(accepted), generation,
                 previous[0]))
            callback_id = previous[0]
        else:
            callback_id = conn.execute(
                'INSERT INTO callbacks(topic,declared_topic,hub,lease_id,generation,body_digest,body,headers,'
                'signature,accepted,last_accepted,checks,reasons,overlap,deliveries,first_seen_at,last_seen_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)',
                (declared_topic, recorded_topic, recorded_hub, lease_id, generation, digest_value, body,
                 json.dumps({k: v for k, v in headers.items()
                             if k in SIGNATURE_HEADERS + DIGEST_HEADERS}, ensure_ascii=False),
                 _header(headers, SIGNATURE_HEADERS), int(accepted), int(accepted), checks_json,
                 reasons_json, int(overlap), now, now)).lastrowid

        if accepted:
            for entry in entries:
                inserted.extend(_insert_notification(conn, declared_topic, callback_id, generation, entry, now))

    deliveries = conn.execute('SELECT deliveries FROM callbacks WHERE id=?', (callback_id,)).fetchone()[0]
    return {'topic': topic, 'body_digest': digest_value, 'accepted': accepted, 'duplicate': duplicate,
            'deliveries': deliveries, 'generation': generation, 'checks': checks, 'reasons': reasons,
            'overlap': overlap, 'notifications': _notification_states(conn, callback_id),
            'new_notifications': inserted if accepted else []}


def _insert_notification(conn, topic, callback_id, generation, entry, now):
    identity = (topic, entry['article_id'], entry['revision'], entry['source_url'], int(entry['deleted']))
    existing = conn.execute(
        'SELECT id,first_generation,first_callback_id FROM notifications WHERE topic=? AND article_id=? '
        'AND revision=? AND source_url=? AND deleted=?', identity).fetchone()
    if existing:
        notification_id, first_generation, first_callback = existing
        created = False
    else:
        notification_id = conn.execute(
            'INSERT INTO notifications(topic,article_id,revision,source_url,deleted,cursor,first_generation,'
            'first_callback_id,first_seen_at,refetch_state) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (topic, entry['article_id'], entry['revision'], entry['source_url'], int(entry['deleted']),
             entry['cursor'], generation, callback_id, now, 'pending')).lastrowid
        first_generation = generation
        first_callback = callback_id
        created = True
    conn.execute(
        'INSERT OR IGNORE INTO notification_receipts(notification_id,callback_id,generation,cursor,delivered_at) '
        'VALUES(?,?,?,?,?)', (notification_id, callback_id, generation, entry['cursor'], now))
    return [{'notification_id': notification_id, 'article_id': entry['article_id'],
             'revision': entry['revision'], 'deleted': entry['deleted'], 'cursor': entry['cursor'],
             'created': created, 'first_generation': first_generation, 'first_callback_id': first_callback}]


def _notification_states(conn, callback_id):
    rows = conn.execute(
        'SELECT n.id,n.article_id,n.revision,n.deleted,n.cursor,n.refetch_state '
        'FROM notifications n JOIN notification_receipts r ON r.notification_id=n.id WHERE r.callback_id=? '
        'ORDER BY n.id', (callback_id,)).fetchall()
    return [{'notification_id': r[0], 'article_id': r[1], 'revision': r[2], 'deleted': bool(r[3]),
             'cursor': r[4], 'refetch_state': r[5]} for r in rows]
