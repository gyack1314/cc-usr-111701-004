"""回调核验、租约代际与重送幂等。"""
import json
import unittest

from capture_data.callback import receive
from capture_data.lease import ensure_topic, register_lease, retire_lease
from capture_data.store import connect
from tests import fixtures

AT = '2026-11-17T10:00:00Z'
EXPIRES = '2026-11-17T11:00:00Z'


def fresh():
    conn = connect(':memory:')
    ensure_topic(conn, fixtures.TOPIC, fixtures.HUB, fixtures.SECRET, AT)
    register_lease(conn, fixtures.TOPIC, fixtures.HUB, 'lease-21', EXPIRES, AT)
    return conn


def one_entry(article='article-7', revision='r2', cursor=5, deleted=False):
    return [{'id': article, 'revision': revision, 'cursor': cursor, 'deleted': deleted,
             'source': 'https://publisher.example.test/posts/' + article}]


class CallbackTests(unittest.TestCase):
    def test_verified_callback_only_creates_candidate(self):
        conn = fresh()
        body, headers = fixtures.callback('lease-21', one_entry())
        outcome = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:00Z')
        self.assertTrue(outcome['accepted'])
        self.assertEqual(outcome['generation'], 1)
        self.assertEqual(outcome['checks']['signature'], True)
        self.assertEqual(outcome['checks']['topic'], True)
        self.assertEqual(outcome['checks']['lease_expiry'], True)
        self.assertEqual(outcome['notifications'][0]['refetch_state'], 'pending')
        # 通知不直接决定文章版本
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
        conn.close()

    def test_bad_signature_rejected_and_redelivery_is_idempotent(self):
        conn = fresh()
        body, headers = fixtures.callback('lease-21', one_entry())
        bad = dict(headers)
        bad['x-hub-signature-256'] = 'sha256=' + '0' * 64
        outcome = receive(conn, fixtures.TOPIC, body, bad, at='2026-11-17T10:01:00Z')
        self.assertFalse(outcome['accepted'])
        self.assertIn('签名与请求原文不符', outcome['reasons'])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 0)

        # 签名恢复后，同一份请求被接受
        good = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:30Z')
        self.assertTrue(good['accepted'])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)

        # 同字节重送不生成第二份通知，delivery 计数保留
        again = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:02:00Z')
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['deliveries'], 3)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_receipts').fetchone()[0], 1)
        conn.close()

    def test_topic_and_hub_are_bound_together(self):
        conn = fresh()
        body, headers = fixtures.callback('lease-21', one_entry(), topic='https://evil.example.test/feed')
        outcome = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:00Z')
        self.assertFalse(outcome['accepted'])
        self.assertIn('回调 topic 与订阅不符', outcome['reasons'])

        body, headers = fixtures.callback('lease-21', one_entry(), hub='https://other-hub.example.test')
        outcome = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:00Z')
        self.assertFalse(outcome['accepted'])
        self.assertIn('回调 hub 与订阅不符', outcome['reasons'])
        conn.close()

    def test_digest_header_mismatch_rejected(self):
        conn = fresh()
        body, headers = fixtures.callback('lease-21', one_entry())
        headers = dict(headers, digest='sha-256=' + 'a' * 44)
        outcome = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:00Z')
        self.assertFalse(outcome['accepted'])
        self.assertFalse(outcome['checks']['content_digest'])
        conn.close()

    def test_lease_expiry_and_unknown_lease(self):
        conn = fresh()
        body, headers = fixtures.callback('lease-99', one_entry())
        outcome = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:00Z')
        self.assertFalse(outcome['accepted'])
        self.assertIn('未知租约', outcome['reasons'])

        body, headers = fixtures.callback('lease-21', one_entry())
        expired = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T12:00:00Z')
        self.assertFalse(expired['accepted'])
        self.assertIn('租约已过期', expired['reasons'])
        conn.close()

    def test_overlap_generations_keep_old_callback_separate(self):
        conn = fresh()
        # 续租前已投递的回调，在新租约后重送
        first_body, first_headers = fixtures.callback('lease-21', one_entry('article-7', 'r2', 5))
        first = receive(conn, fixtures.TOPIC, first_body, first_headers, at='2026-11-17T10:05:00Z')
        self.assertEqual(first['generation'], 1)
        self.assertFalse(first['overlap'])

        registered = register_lease(conn, fixtures.TOPIC, fixtures.HUB, 'lease-22',
                                    '2026-11-17T12:00:00Z', '2026-11-17T10:10:00Z')
        self.assertEqual(registered['generation'], 2)
        self.assertTrue(registered['overlap'])
        self.assertEqual(registered['overlapping_generations'], [1])

        late = receive(conn, fixtures.TOPIC, first_body, first_headers, at='2026-11-17T10:11:00Z')
        self.assertTrue(late['duplicate'])
        self.assertEqual(late['generation'], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)

        new_body, new_headers = fixtures.callback('lease-22', one_entry('article-8', 'r1', 6))
        new = receive(conn, fixtures.TOPIC, new_body, new_headers, at='2026-11-17T10:12:00Z')
        self.assertEqual(new['generation'], 2)
        self.assertFalse(new['overlap'])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 2)

        leases = {r[0]: (r[1], r[2]) for r in conn.execute(
            'SELECT generation,state,superseded_by FROM leases').fetchall()}
        self.assertEqual(leases[1], ('overlapping', 2))
        self.assertEqual(leases[2], ('active', None))
        conn.close()

    def test_retired_lease_rejects_new_notifications(self):
        conn = fresh()
        retire_lease(conn, fixtures.TOPIC, 1, '2026-11-17T10:06:00Z')
        body, headers = fixtures.callback('lease-21', one_entry())
        outcome = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:07:00Z')
        self.assertFalse(outcome['accepted'])
        self.assertIn('租约已停用', outcome['reasons'])
        conn.close()

    def test_declared_expiry_cannot_exceed_registered_lease(self):
        conn = fresh()
        body, headers = fixtures.callback('lease-21', one_entry(), expires='2026-11-17T18:00:00Z')
        outcome = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:00Z')
        self.assertFalse(outcome['accepted'])
        self.assertIn('回调声明有效期超出登记租约', outcome['reasons'])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 0)
        conn.close()

    def test_reregistering_retired_lease_does_not_revive_it(self):
        conn = fresh()
        retire_lease(conn, fixtures.TOPIC, 1, '2026-11-17T10:06:00Z')
        again = register_lease(conn, fixtures.TOPIC, fixtures.HUB, 'lease-21',
                               '2026-11-17T13:00:00Z', '2026-11-17T10:07:00Z')
        self.assertEqual(again['generation'], 1)
        self.assertEqual(again['state'], 'retired')
        body, headers = fixtures.callback('lease-21', one_entry())
        outcome = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:08:00Z')
        self.assertFalse(outcome['accepted'])
        self.assertIn('租约已停用', outcome['reasons'])
        conn.close()

    def test_restart_preserves_evidence_and_dedup(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'journal.sqlite3'
            body, headers = fixtures.callback('lease-21', one_entry())
            conn = connect(path)
            ensure_topic(conn, fixtures.TOPIC, fixtures.HUB, fixtures.SECRET, AT)
            register_lease(conn, fixtures.TOPIC, fixtures.HUB, 'lease-21', EXPIRES, AT)
            receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:00Z')
            conn.close()

            conn = connect(path)
            again = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:02:00Z')
            self.assertTrue(again['duplicate'])
            self.assertEqual(again['deliveries'], 2)
            self.assertEqual(again['generation'], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM callbacks').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)
            conn.close()

    def test_receipts_track_each_generation(self):
        conn = fresh()
        first_body, first_headers = fixtures.callback('lease-21', one_entry())
        receive(conn, fixtures.TOPIC, first_body, first_headers, at='2026-11-17T10:05:00Z')
        register_lease(conn, fixtures.TOPIC, fixtures.HUB, 'lease-22', '2026-11-17T12:00:00Z',
                       '2026-11-17T10:10:00Z')
        # 新代际重送同一通知：不新增通知，但记录第二次送达
        second_body, second_headers = fixtures.callback('lease-22', one_entry())
        receive(conn, fixtures.TOPIC, second_body, second_headers, at='2026-11-17T10:11:00Z')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)
        receipts = conn.execute('SELECT DISTINCT generation FROM notification_receipts ORDER BY generation').fetchall()
        self.assertEqual(receipts, [(1,), (2,)])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_receipts').fetchone()[0], 2)
        conn.close()
