"""换 hub、服务重启与运营报告的最终一致性。"""
import json
import tempfile
import unittest
from pathlib import Path

from capture_data.callback import receive
from capture_data.lease import ensure_topic, register_lease, set_hub
from capture_data.report import build
from capture_data.source import article_states, coverage, receive_page
from capture_data.store import connect
from tests import fixtures

AT = '2026-11-17T10:00:00Z'
NEW_HUB = 'https://hub2.example.test'


def fresh():
    conn = connect(':memory:')
    ensure_topic(conn, fixtures.TOPIC, fixtures.HUB, fixtures.SECRET, AT)
    register_lease(conn, fixtures.TOPIC, fixtures.HUB, 'lease-21', '2026-11-17T11:00:00Z', AT)
    return conn


def ev(article, revision, cursor, deleted=False):
    return fixtures.event(article, revision, cursor, deleted)


class ConsistencyTests(unittest.TestCase):
    def test_hub_switch_keeps_old_generation_evidence_and_rejects_old_hub_new_body(self):
        conn = fresh()
        body, headers = fixtures.callback('lease-21', [{'id': 'article-7', 'revision': 'r2',
                                                        'source': 'https://publisher.example.test/posts/article-7',
                                                        'deleted': False, 'cursor': 5}])
        receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:00Z')
        before = build(conn, fixtures.TOPIC)

        set_hub(conn, fixtures.TOPIC, NEW_HUB, fixtures.SECRET, '2026-11-17T10:05:00Z')
        set_hub(conn, fixtures.TOPIC, NEW_HUB, fixtures.SECRET, '2026-11-17T10:05:00Z')

        after = build(conn, fixtures.TOPIC)
        self.assertEqual(after['hub'], NEW_HUB)
        self.assertEqual(after['articles'], before['articles'])
        self.assertEqual(after['source'], before['source'])
        self.assertEqual(after['notifications'], before['notifications'])
        # 旧 hub 的回调在换 hub 后不能作为新通知入账
        outcome = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:06:00Z')
        self.assertFalse(outcome['accepted'])
        self.assertIn('回调 hub 与订阅不符', outcome['reasons'])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)
        conn.close()

    def test_report_lists_unfilled_gaps_for_operators(self):
        conn = fresh()
        receive_page(conn, fixtures.TOPIC, fixtures.page(0, 10, [ev('a', 'r1', 3)]),
                     at='2026-11-17T10:01:00Z')
        receive_page(conn, fixtures.TOPIC, fixtures.page(20, 30, [ev('b', 'r1', 22)]),
                     at='2026-11-17T10:02:00Z')
        report = build(conn, fixtures.TOPIC)
        self.assertEqual(report['source']['watermark'], 10)
        self.assertEqual(report['unresolved_gaps'], [{'cursor_start': 10, 'cursor_end': 20}])
        self.assertTrue(any(item['kind'] == 'coverage-gap' for item in report['anomalies']))
        self.assertTrue(any(item['kind'] == 'tail-open' for item in report['anomalies']))
        conn.close()

    def test_report_is_stable_across_restart_and_redelivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'journal.sqlite3'
            conn = connect(path)
            ensure_topic(conn, fixtures.TOPIC, fixtures.HUB, fixtures.SECRET, AT)
            register_lease(conn, fixtures.TOPIC, fixtures.HUB, 'lease-21', '2026-11-17T11:00:00Z', AT)
            body, headers = fixtures.callback('lease-21', [{'id': 'a', 'revision': 'r1',
                                                            'source': 'https://publisher.example.test/posts/a',
                                                            'deleted': False, 'cursor': 3}])
            receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:00Z')
            receive_page(conn, fixtures.TOPIC, fixtures.page(0, 10, [ev('a', 'r1', 3)]),
                         at='2026-11-17T10:02:00Z')
            first = build(conn, fixtures.TOPIC)
            conn.close()

            conn = connect(path)
            redelivered = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:03:00Z')
            self.assertTrue(redelivered['duplicate'])
            second = build(conn, fixtures.TOPIC)
            # 重送只增加投递次数，不改变文章状态、通知与证据指纹
            self.assertEqual(second['fingerprint'], first['fingerprint'])
            self.assertEqual(second['articles'], first['articles'])
            self.assertEqual(second['notifications'], first['notifications'])
            self.assertEqual(second['source'], first['source'])
            conn.close()

    def test_final_article_state_consistent_with_evidence(self):
        conn = fresh()
        # 回调说 article-7 有 r2，但源站回补后来证明 r2 已撤稿
        body, headers = fixtures.callback('lease-21', [
            {'id': 'article-7', 'revision': 'r2', 'source': 'https://publisher.example.test/posts/article-7',
             'deleted': False, 'cursor': 5}])
        receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:00Z')
        receive_page(conn, fixtures.TOPIC,
                     fixtures.page(0, 10, [ev('article-7', 'r2', 5), ev('article-7', 'r3', 6, deleted=True)]),
                     at='2026-11-17T10:02:00Z')
        states = article_states(conn, fixtures.TOPIC)
        report = build(conn, fixtures.TOPIC)
        self.assertEqual(states[0]['state'], 'withdrawn')
        self.assertEqual(report['articles'][0]['state'], 'withdrawn')
        # 通知仍被保留为证据，但状态由源站证据给出
        self.assertEqual(report['notifications'][0]['refetch_state'], 'confirmed')
        self.assertEqual(report['notification_states'], {'confirmed': 1})
        conn.close()

    def test_rejected_callbacks_and_pages_visible_in_report(self):
        conn = fresh()
        body, headers = fixtures.callback('lease-21', [{'id': 'a', 'revision': 'r1',
                                                        'source': 'https://publisher.example.test/posts/a',
                                                        'deleted': False, 'cursor': 3}])
        bad = dict(headers, **{'x-hub-signature-256': 'sha256=' + 'f' * 64})
        receive(conn, fixtures.TOPIC, body, bad, at='2026-11-17T10:01:00Z')
        receive_page(conn, fixtures.TOPIC, fixtures.page(0, 10, [ev('a', 'r1', 3)]),
                     at='2026-11-17T10:02:00Z')
        receive_page(conn, fixtures.TOPIC, fixtures.page(0, 5, [ev('a', 'r9', 3)]),
                     at='2026-11-17T10:03:00Z')
        report = build(conn, fixtures.TOPIC)
        self.assertEqual(report['callbacks']['rejected'], 1)
        self.assertEqual(report['callbacks']['accepted'], 0)
        self.assertEqual(report['callbacks']['redelivered'], 0)
        self.assertEqual(len(report['rejected_callbacks']), 1)
        self.assertTrue(any(item['kind'] == 'rejected-callback' for item in report['anomalies']))
        self.assertTrue(any(item['kind'] == 'rejected-page' for item in report['anomalies']))
        self.assertEqual(len(report['pages']), 2)
        self.assertEqual(coverage(conn, fixtures.TOPIC)['watermark'], 10)
        conn.close()

    def test_lease_generation_overlap_visible_in_report(self):
        conn = fresh()
        register_lease(conn, fixtures.TOPIC, fixtures.HUB, 'lease-22', '2026-11-17T12:00:00Z',
                       '2026-11-17T10:05:00Z')
        body, headers = fixtures.callback('lease-21', [{'id': 'a', 'revision': 'r1',
                                                        'source': 'https://publisher.example.test/posts/a',
                                                        'deleted': False, 'cursor': 3}])
        outcome = receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:06:00Z')
        self.assertTrue(outcome['overlap'])
        report = build(conn, fixtures.TOPIC)
        self.assertEqual(report['active_generation'], 2)
        self.assertEqual([lease['state'] for lease in report['leases']], ['overlapping', 'active'])
        self.assertTrue(any(item['kind'] == 'overlap-notification' for item in report['anomalies']))
        conn.close()
