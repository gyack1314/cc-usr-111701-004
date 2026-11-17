"""运营视图：目前哪些空档未补齐、哪些通知未获源站证实、证据是否自洽。

报告只由已落盘证据计算，同一账本与同一输入产生相同 fingerprint，
便于服务重启、hub 重送和换 hub 后对比决定是否发生矛盾。
"""
import hashlib
import json

from .source import article_states, coverage


def build(conn, topic):
    subscription = conn.execute('SELECT topic,hub,active_generation,watermark FROM subscriptions WHERE topic=?',
                                (topic,)).fetchone()
    if not subscription:
        raise ValueError('未知订阅')
    hub, active_generation, _ = subscription[1], subscription[2], subscription[3]

    leases = [{'generation': r[0], 'hub': r[1], 'lease_id': r[2], 'expires_at': r[3],
               'state': r[4], 'superseded_by': r[5]}
              for r in conn.execute('SELECT generation,hub,lease_id,expires_at,state,superseded_by '
                                    'FROM leases WHERE topic=? ORDER BY generation', (topic,))]

    callback_counts = {'accepted': 0, 'rejected': 0, 'recovered': 0, 'redelivered': 0}
    rejected = []
    for r in conn.execute('SELECT id,body_digest,generation,accepted,last_accepted,deliveries,reasons,overlap '
                          'FROM callbacks WHERE topic=? ORDER BY id', (topic,)):
        if r[4]:
            callback_counts['accepted'] += 1
            if not r[3]:
                callback_counts['recovered'] += 1
        else:
            callback_counts['rejected'] += 1
            rejected.append({'callback_id': r[0], 'body_digest': r[1], 'generation': r[2],
                             'deliveries': r[5], 'reasons': json.loads(r[6])})
        if r[5] > 1:
            callback_counts['redelivered'] += 1

    notifications = []
    for r in conn.execute('SELECT id,article_id,revision,source_url,deleted,cursor,first_generation,refetch_state '
                          'FROM notifications WHERE topic=? ORDER BY id', (topic,)):
        notifications.append({'notification_id': r[0], 'article_id': r[1], 'revision': r[2],
                              'source_url': r[3], 'deleted': bool(r[4]), 'cursor': r[5],
                              'first_generation': r[6], 'refetch_state': r[7]})
    next_states = {}
    for item in notifications:
        next_states.setdefault(item['refetch_state'], 0)
        next_states[item['refetch_state']] += 1

    pages = [{'digest': r[0], 'cursor_start': r[1], 'cursor_end': r[2], 'accepted': bool(r[3]),
              'rejection': json.loads(r[4]) if r[4] else None}
             for r in conn.execute('SELECT digest,cursor_start,cursor_end,accepted,rejection FROM source_pages '
                                   'WHERE topic=? ORDER BY cursor_start,cursor_end,digest', (topic,))]

    gaps = coverage(conn, topic)
    articles = article_states(conn, topic)

    anomalies = []
    if gaps['gaps']:
        anomalies.append({'kind': 'coverage-gap', 'gaps': gaps['gaps'],
                          'detail': '连续水位之后仍有未接收区间'})
    if gaps['tail_open']:
        anomalies.append({'kind': 'tail-open', 'detail': '源站尾部尚无结束标记'})
    pending = [n['article_id'] for n in notifications if n['refetch_state'] == 'pending']
    if pending:
        anomalies.append({'kind': 'unrefuted-notifications', 'article_ids': sorted(set(pending)),
                          'detail': '通知尚未在源站证据中证实或推翻'})
    for item in rejected:
        anomalies.append({'kind': 'rejected-callback', 'callback_id': item['callback_id'],
                          'reasons': item['reasons']})
    for page in pages:
        if not page['accepted']:
            anomalies.append({'kind': 'rejected-page', 'digest': page['digest'], 'rejection': page['rejection']})
    for notification in notifications:
        if notification['first_generation'] not in (None, active_generation):
            anomalies.append({'kind': 'overlap-notification', 'notification_id': notification['notification_id'],
                              'generation': notification['first_generation'],
                              'active_generation': active_generation,
                              'detail': '续租交叠期间的旧代际通知'})

    report = {
        'topic': topic,
        'hub': hub,
        'active_generation': active_generation,
        'leases': leases,
        'callbacks': callback_counts,
        'rejected_callbacks': rejected,
        'notifications': notifications,
        'notification_states': next_states,
        'source': gaps,
        'pages': pages,
        'articles': articles,
        'unresolved_gaps': gaps['gaps'],
        'anomalies': anomalies,
    }
    report['fingerprint'] = fingerprint(report)
    return report


def fingerprint(report):
    """对决定与证据做规范化摘要；不含接收时刻与投递次数，重送与重启不改变结果。"""
    material = {
        'topic': report['topic'],
        'hub': report['hub'],
        'active_generation': report['active_generation'],
        'leases': report['leases'],
        'callbacks': {key: report['callbacks'][key] for key in ('accepted', 'rejected', 'recovered')},
        'rejected_callbacks': [{'callback_id': r['callback_id'], 'body_digest': r['body_digest'],
                                'generation': r['generation'], 'reasons': r['reasons']}
                               for r in report['rejected_callbacks']],
        'notifications': report['notifications'],
        'source': report['source'],
        'pages': report['pages'],
        'articles': report['articles'],
        'unresolved_gaps': report['unresolved_gaps'],
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()
