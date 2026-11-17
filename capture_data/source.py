"""源站回补：分页乱序接收、原始证据留存、连续覆盖水位。

分页与更新游标可以和回调乱序到达。水位只在区间能连续衔接时推进，
缺口保持可见；文章当前状态只由源站事件按游标推导，通知不参与定稿。
"""
import hashlib
import json
from datetime import datetime, timezone

from .lease import iso, utc


def _now(at):
    return iso(utc(at)) if at else datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _int(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(name + ' 必须为非负整数')
    return value


def parse_page(body):
    """解析并校验源站分页；事件游标必须落在 [cursor_start, cursor_end)。"""
    try:
        doc = json.loads(body.decode('utf-8') if isinstance(body, (bytes, bytearray)) else body)
    except Exception as exc:
        raise ValueError('源站分页不是合法 JSON:' + str(exc))
    if not isinstance(doc, dict):
        raise ValueError('源站分页必须为对象')
    topic = doc.get('topic')
    if not isinstance(topic, str) or not topic:
        raise ValueError('源站分页缺少 topic')
    start = _int(doc.get('cursor_start'), 'cursor_start')
    raw_events = doc.get('events')
    if not isinstance(raw_events, list):
        raise ValueError('events 必须为数组')
    events = []
    for item in raw_events:
        if not isinstance(item, dict):
            raise ValueError('源站事件必须为对象')
        article = item.get('id')
        revision = item.get('revision')
        source = item.get('source', '')
        deleted = item.get('deleted', False)
        cursor = _int(item.get('cursor'), '事件游标')
        if not isinstance(article, str) or not article:
            raise ValueError('源站事件缺少文章标识')
        if not isinstance(revision, str) or not revision:
            raise ValueError('源站事件缺少修订标识')
        if not isinstance(source, str) or (source and not source.startswith(('http://', 'https://'))):
            raise ValueError('源站事件来源地址非法')
        if not isinstance(deleted, bool):
            raise ValueError('源站事件撤稿标记非法')
        if cursor < start:
            raise ValueError('事件游标落在分页起点之前')
        events.append({'article_id': article, 'revision': revision, 'source_url': source,
                       'deleted': deleted, 'cursor': cursor})
    next_cursor = doc.get('next_cursor')
    if next_cursor is not None:
        next_cursor = _int(next_cursor, 'next_cursor')
    declared_end = doc.get('cursor_end')
    if declared_end is not None:
        declared_end = _int(declared_end, 'cursor_end')
        if next_cursor is not None and declared_end != next_cursor:
            raise ValueError('cursor_end 与 next_cursor 不一致')
    end_of_feed = bool(doc.get('end_of_feed', False))
    highest = max((e['cursor'] for e in events), default=None)
    if next_cursor is not None:
        if highest is not None and next_cursor <= highest:
            raise ValueError('next_cursor 未越过本页事件')
        end = next_cursor
    elif highest is not None:
        end = highest + 1
    elif end_of_feed:
        end = start
    else:
        raise ValueError('空分页既无 next_cursor 也无结束标记')
    if end < start:
        raise ValueError('分页区间非法')
    if declared_end is not None and declared_end != end:
        raise ValueError('cursor_end 与推导区间不一致')
    if any(e['cursor'] >= end for e in events):
        raise ValueError('事件游标超出分页区间')
    by_cursor = {}
    for event in events:
        identity = (event['article_id'], event['revision'], event['source_url'], event['deleted'])
        other = by_cursor.setdefault(event['cursor'], identity)
        if other != identity:
            raise ValueError('同页游标指向冲突的事件')
    def _sort_key(event):
        return (event['cursor'], event['article_id'], event['revision'],
                event['source_url'], int(event['deleted']))

    events.sort(key=_sort_key)
    canonical = {'topic': topic, 'cursor_start': start, 'cursor_end': end,
                 'events': events, 'next_cursor': next_cursor, 'end_of_feed': end_of_feed}
    return canonical


def _event_digest(topic, event):
    material = json.dumps([topic, event['cursor'], event['article_id'], event['revision'],
                           event['source_url'], event['deleted']], ensure_ascii=False)
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def receive_page(conn, topic, body, at=None):
    """接收一页源站资料；重复页与重叠页幂等，矛盾游标拒绝且不推进水位。"""
    subscription = conn.execute('SELECT topic FROM subscriptions WHERE topic=?', (topic,)).fetchone()
    if not subscription:
        raise ValueError('未知订阅')
    raw = body.encode('utf-8') if isinstance(body, str) else bytes(body)
    declared = parse_page(raw)
    if declared['topic'] != topic:
        raise ValueError('源站分页 topic 与订阅不符')
    digest = hashlib.sha256(raw).hexdigest()
    now = _now(at)

    previous = conn.execute('SELECT id,accepted FROM source_pages WHERE topic=? AND digest=?', (topic, digest)).fetchone()
    if previous and previous[1]:
        return {'topic': topic, 'digest': digest, 'duplicate': True, 'accepted': True,
                'inserted_events': 0, 'coverage': coverage(conn, topic)}

    conflict = None
    for event in declared['events']:
        row = conn.execute('SELECT article_id,revision,source_url,deleted,page_digest FROM source_events '
                           'WHERE topic=? AND event_cursor=?', (topic, event['cursor'])).fetchone()
        if row and (row[0], row[1], row[2], bool(row[3])) != (
                event['article_id'], event['revision'], event['source_url'], event['deleted']):
            conflict = {'cursor': event['cursor'], 'existing_page': row[4], 'page_digest': digest}
            break
    if conflict:
        with conn:
            conn.execute('INSERT OR IGNORE INTO source_pages(topic,declared_topic,cursor_start,cursor_end,complete,'
                         'end_of_feed,next_cursor,digest,raw,accepted,rejection,received_at) '
                         'VALUES(?,?,?,?,0,0,?,?,?,0,?,?)',
                         (topic, declared['topic'], declared['cursor_start'], declared['cursor_end'],
                          declared['next_cursor'], digest, raw,
                          json.dumps({'reason': '游标证据冲突', **conflict}, ensure_ascii=False), now))
        return {'topic': topic, 'digest': digest, 'duplicate': False, 'accepted': False,
                'rejected': '游标证据冲突', 'conflict': conflict,
                'inserted_events': 0, 'coverage': coverage(conn, topic)}

    with conn:
        page_id = conn.execute(
            'INSERT INTO source_pages(topic,declared_topic,cursor_start,cursor_end,complete,end_of_feed,'
            'next_cursor,digest,raw,accepted,rejection,received_at) VALUES(?,?,?,?,1,?,?,?,?,1,NULL,?)',
            (topic, declared['topic'], declared['cursor_start'], declared['cursor_end'],
             int(declared['end_of_feed']), declared['next_cursor'], digest, raw, now)).lastrowid
        inserted = 0
        for event in declared['events']:
            event_digest = _event_digest(topic, event)
            inserted += conn.execute(
                'INSERT OR IGNORE INTO source_events(topic,article_id,revision,source_url,deleted,event_cursor,'
                'page_digest,event_digest,observed_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (topic, event['article_id'], event['revision'], event['source_url'], int(event['deleted']),
                 event['cursor'], digest, event_digest, now)).rowcount
        _refresh_watermark(conn, topic)
        _refresh_notifications(conn, topic)

    return {'topic': topic, 'digest': digest, 'duplicate': False, 'accepted': True,
            'page_id': page_id, 'inserted_events': inserted, 'rejected': None,
            'coverage': coverage(conn, topic)}


def _intervals(conn, topic):
    rows = conn.execute('SELECT cursor_start,cursor_end,end_of_feed FROM source_pages WHERE topic=? AND accepted=1 '
                        'ORDER BY cursor_start,cursor_end', (topic,)).fetchall()
    merged = []
    for start, end, _ in rows:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged, any(bool(r[2]) for r in rows)


def _refresh_watermark(conn, topic):
    merged, _ = _intervals(conn, topic)
    watermark = 0
    for start, end in merged:
        if start > watermark:
            break
        watermark = max(watermark, end)
    conn.execute('UPDATE subscriptions SET watermark=?,updated_at=? WHERE topic=?', (watermark, _now(None), topic))
    return watermark


def coverage(conn, topic):
    """返回连续水位、已覆盖区间和仍空着的区间。"""
    merged, end_of_feed = _intervals(conn, topic)
    watermark = conn.execute('SELECT watermark FROM subscriptions WHERE topic=?', (topic,)).fetchone()[0]
    frontier = max((end for _, end in merged), default=0)
    gaps = []
    cursor = watermark
    for start, end in merged:
        if end <= cursor:
            continue
        if start > cursor:
            gaps.append({'cursor_start': cursor, 'cursor_end': start})
        cursor = max(cursor, end)
    if cursor < frontier:
        gaps.append({'cursor_start': cursor, 'cursor_end': frontier})
    return {'watermark': watermark, 'frontier': frontier, 'intervals': [list(r) for r in merged],
            'gaps': gaps, 'end_of_feed': end_of_feed,
            'tail_open': not (end_of_feed and watermark >= frontier),
            'closed': bool(end_of_feed and not gaps and watermark >= frontier)}


def _page_covering(conn, topic, cursor):
    return conn.execute(
        'SELECT 1 FROM source_pages WHERE topic=? AND accepted=1 AND cursor_start<=? AND cursor_end>? LIMIT 1',
        (topic, cursor, cursor)).fetchone() is not None


def _refresh_notifications(conn, topic):
    rows = conn.execute('SELECT id,article_id,revision,source_url,deleted,cursor FROM notifications WHERE topic=?',
                        (topic,)).fetchall()
    for notification_id, article, revision, source, deleted, cursor in rows:
        match = conn.execute('SELECT 1 FROM source_events WHERE topic=? AND article_id=? AND revision=? '
                             'AND source_url=? AND deleted=? LIMIT 1',
                             (topic, article, revision, source, 1 if deleted else 0)).fetchone()
        if match:
            state = 'confirmed'
        elif cursor is not None and _page_covering(conn, topic, cursor):
            state = 'refuted'
        else:
            state = 'pending'
        conn.execute('UPDATE notifications SET refetch_state=? WHERE id=?', (state, notification_id))


def article_states(conn, topic):
    """按源站游标推导文章状态；通知和回调不参与，证据全部保留。

    较低游标的后续送达不会覆盖较高游标，因此晚到的旧页不会复活已撤稿文章。
    同游标互斥证据在接收阶段即被拒收，因此文章状态只由已接受的证据推导。
    """
    watermark = conn.execute('SELECT watermark FROM subscriptions WHERE topic=?', (topic,)).fetchone()[0]
    rows = conn.execute('SELECT article_id,revision,source_url,deleted,event_cursor,page_digest,event_digest '
                        'FROM source_events WHERE topic=? ORDER BY article_id,event_cursor,event_digest', (topic,)).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)
    states = []
    for article, evidence in grouped.items():
        current = max(evidence, key=lambda r: (r[4], int(r[3]), r[6]))
        states.append({
            'article_id': article,
            'state': 'withdrawn' if current[3] else 'published',
            'current_revision': current[1],
            'source_url': current[2],
            'cursor': current[4],
            'settled': current[4] < watermark,
            'evidence': [{'revision': r[1], 'source_url': r[2], 'deleted': bool(r[3]),
                          'cursor': r[4], 'page_digest': r[5]} for r in evidence],
        })
    states.sort(key=lambda s: s['article_id'])
    return states
