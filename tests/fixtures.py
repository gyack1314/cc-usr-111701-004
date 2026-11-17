"""合成 WebSub 回调与源站分页样例；仅用于离线测试，不访问真实站点。"""
import hashlib
import hmac
import json

TOPIC = 'https://publisher.example.test/feed'
HUB = 'https://hub.example.test'
SECRET = 'synthetic-shared-secret'
LEASE_EXPIRES = '2026-11-17T11:00:00Z'


def sign(body):
    return 'sha256=' + hmac.new(SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()


def callback(lease_id, entries, signature=None, expires=LEASE_EXPIRES, topic=TOPIC, hub=HUB):
    doc = {'hub': hub, 'topic': topic, 'lease_id': lease_id, 'lease_expires': expires,
           'signature': 'sha256=synthetic', 'entries': entries}
    body = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return body, {'x-hub-signature-256': signature if signature is not None else sign(body)}


def page(start, end, events, end_of_feed=False):
    return json.dumps({'topic': TOPIC, 'cursor_start': start, 'cursor_end': end,
                       'events': events, 'next_cursor': end, 'end_of_feed': end_of_feed},
                      ensure_ascii=False, sort_keys=True).encode('utf-8')


def event(article, revision, cursor, deleted=False, source=None):
    return {'id': article, 'revision': revision, 'cursor': cursor, 'deleted': deleted,
            'source': source if source is not None else 'https://publisher.example.test/posts/' + article}
