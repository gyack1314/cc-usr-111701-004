"""源站分页回补：乱序、缺口、撤稿证据与连续水位。"""
import json
import tempfile
import unittest
from pathlib import Path

from capture_data.lease import ensure_topic, register_lease
from capture_data.source import article_states, coverage, parse_page, receive_page
from capture_data.store import connect
from tests import fixtures

AT = '2026-11-17T10:00:00Z'


def fresh():
    conn = connect(':memory:')
    ensure_topic(conn, fixtures.TOPIC, fixtures.HUB, fixtures.SECRET, AT)
    register_lease(conn, fixtures.TOPIC, fixtures.HUB, 'lease-21', '2026-11-17T11:00:00Z', AT)
    return conn


def ev(article, revision, cursor, deleted=False):
    return fixtures.event(article, revision, cursor, deleted)


class SourceTests(unittest.TestCase):
    def test_out_of_order_pages_advance_watermark_only_when_contiguous(self):
        conn = fresh()
        late = receive_page(conn, fixtures.TOPIC, fixtures.page(10, 20, [ev('article-9', 'r1', 12)]),
                            at='2026-11-17T10:02:00Z')
        self.assertTrue(late['accepted'])
        self.assertEqual(late['coverage']['watermark'], 0)
        self.assertEqual(late['coverage']['gaps'], [{'cursor_start': 0, 'cursor_end': 10}])
        self.assertEqual(late['coverage']['frontier'], 20)

        early = receive_page(conn, fixtures.TOPIC, fixtures.page(0, 20, [ev('article-7', 'r2', 5)]),
                             at='2026-11-17T10:03:00Z')
        self.assertEqual(early['coverage']['watermark'], 20)
        self.assertEqual(early['coverage']['gaps'], [])
        self.assertTrue(early['coverage']['tail_open'])  # 尚无结束标记
        final = receive_page(conn, fixtures.TOPIC, fixtures.page(20, 20, [], end_of_feed=True),
                             at='2026-11-17T10:04:00Z')
        self.assertFalse(final['coverage']['tail_open'])
        self.assertTrue(final['coverage']['closed'])
        conn.close()

    def test_tail_open_until_end_of_feed(self):
        conn = fresh()
        receive_page(conn, fixtures.TOPIC, fixtures.page(0, 10, [ev('a', 'r1', 3)]),
                     at='2026-11-17T10:01:00Z')
        view = coverage(conn, fixtures.TOPIC)
        self.assertEqual(view['watermark'], 10)
        self.assertTrue(view['tail_open'])
        self.assertFalse(view['closed'])

        receive_page(conn, fixtures.TOPIC, fixtures.page(10, 10, [], end_of_feed=True),
                     at='2026-11-17T10:02:00Z')
        view = coverage(conn, fixtures.TOPIC)
        self.assertFalse(view['tail_open'])
        self.assertTrue(view['closed'])
        conn.close()

    def test_duplicate_and_overlapping_pages_are_idempotent(self):
        conn = fresh()
        payload = fixtures.page(0, 10, [ev('a', 'r1', 3), ev('b', 'r1', 7)])
        first = receive_page(conn, fixtures.TOPIC, payload, at='2026-11-17T10:01:00Z')
        self.assertEqual(first['inserted_events'], 2)
        again = receive_page(conn, fixtures.TOPIC, payload, at='2026-11-17T10:02:00Z')
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['inserted_events'], 0)

        overlap = fixtures.page(5, 15, [ev('b', 'r1', 7), ev('c', 'r1', 11)])
        merged = receive_page(conn, fixtures.TOPIC, overlap, at='2026-11-17T10:03:00Z')
        self.assertEqual(merged['inserted_events'], 1)
        self.assertEqual(merged['coverage']['watermark'], 15)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_events').fetchone()[0], 3)
        conn.close()

    def test_conflicting_cursor_evidence_rejected_without_advancing(self):
        conn = fresh()
        receive_page(conn, fixtures.TOPIC, fixtures.page(0, 10, [ev('a', 'r1', 3)]),
                     at='2026-11-17T10:01:00Z')
        before = coverage(conn, fixtures.TOPIC)
        conflict = fixtures.page(0, 10, [ev('a', 'r9', 3)])
        outcome = receive_page(conn, fixtures.TOPIC, conflict, at='2026-11-17T10:02:00Z')
        self.assertFalse(outcome['accepted'])
        self.assertEqual(outcome['rejected'], '游标证据冲突')
        self.assertEqual(outcome['coverage'], before)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_events').fetchone()[0], 1)
        # 原始冲突证据保留，供运营核查
        raw = conn.execute('SELECT raw,rejection FROM source_pages WHERE accepted=0').fetchall()
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0][0], conflict)
        conn.close()

    def test_withdrawal_survives_late_lower_cursor_page(self):
        conn = fresh()
        # 高游标撤稿先到
        receive_page(conn, fixtures.TOPIC, fixtures.page(10, 20, [ev('a', 'r1', 10), ev('a', 'r2', 11, True)]),
                     at='2026-11-17T10:01:00Z')
        receive_page(conn, fixtures.TOPIC, fixtures.page(0, 20, [ev('a', 'r1', 10)]),
                     at='2026-11-17T10:02:00Z')
        states = article_states(conn, fixtures.TOPIC)
        self.assertEqual(states[0]['state'], 'withdrawn')
        self.assertEqual(states[0]['current_revision'], 'r2')
        self.assertEqual([e['revision'] for e in states[0]['evidence']], ['r1', 'r2'])
        self.assertTrue(all(e['deleted'] is False for e in states[0]['evidence'][:1]))
        conn.close()

    def test_revision_evidence_retained_not_overwritten(self):
        conn = fresh()
        receive_page(conn, fixtures.TOPIC, fixtures.page(0, 10, [ev('a', 'r1', 3)]),
                     at='2026-11-17T10:01:00Z')
        receive_page(conn, fixtures.TOPIC, fixtures.page(10, 20, [ev('a', 'r2', 12)]),
                     at='2026-11-17T10:02:00Z')
        states = article_states(conn, fixtures.TOPIC)
        self.assertEqual(states[0]['state'], 'published')
        self.assertEqual(states[0]['current_revision'], 'r2')
        self.assertEqual(len(states[0]['evidence']), 2)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_events').fetchone()[0], 2)
        conn.close()

    def test_same_cursor_conflict_keeps_first_evidence(self):
        conn = fresh()
        receive_page(conn, fixtures.TOPIC, fixtures.page(0, 10, [ev('a', 'r1', 3)]),
                     at='2026-11-17T10:01:00Z')
        rejected = receive_page(conn, fixtures.TOPIC, fixtures.page(0, 6, [ev('a', 'r2', 3)]),
                                at='2026-11-17T10:02:00Z')
        self.assertFalse(rejected['accepted'])
        states = article_states(conn, fixtures.TOPIC)
        self.assertEqual(states[0]['state'], 'published')
        self.assertEqual(states[0]['current_revision'], 'r1')
        self.assertEqual(len(states[0]['evidence']), 1)
        conn.close()

    def test_notification_confirmed_refuted_or_pending(self):
        from capture_data.callback import receive
        from capture_data.report import build
        conn = fresh()
        body, headers = fixtures.callback('lease-21', [
            {'id': 'article-7', 'revision': 'r2', 'source': 'https://publisher.example.test/posts/article-7',
             'deleted': False, 'cursor': 5},
            {'id': 'article-8', 'revision': 'r1', 'source': 'https://publisher.example.test/posts/article-8',
             'deleted': True, 'cursor': 25}])
        receive(conn, fixtures.TOPIC, body, headers, at='2026-11-17T10:01:00Z')
        receive_page(conn, fixtures.TOPIC,
                     fixtures.page(0, 10, [ev('article-7', 'r2', 5), ev('article-9', 'r1', 7)]),
                     at='2026-11-17T10:02:00Z')
        states = {n['article_id']: n['refetch_state'] for n in build(conn, fixtures.TOPIC)['notifications']}
        self.assertEqual(states['article-7'], 'confirmed')
        self.assertEqual(states['article-8'], 'pending')  # 游标 25 尚未被覆盖
        conn.close()

    def test_parse_page_rejects_bad_cursor_bounds(self):
        with self.assertRaises(ValueError):
            parse_page(json.dumps({'topic': fixtures.TOPIC, 'cursor_start': 10,
                                   'events': [dict(ev('a', 'r1', 5))], 'next_cursor': 20}).encode())
        with self.assertRaises(ValueError):
            parse_page(json.dumps({'topic': fixtures.TOPIC, 'cursor_start': 0,
                                   'events': [ev('a', 'r1', 3)], 'next_cursor': 3}).encode())

    def test_page_topic_must_match_subscription(self):
        conn = fresh()
        payload = json.dumps({'topic': 'https://evil.example.test/feed', 'cursor_start': 0,
                              'events': [ev('a', 'r1', 3)], 'next_cursor': 10}).encode()
        with self.assertRaises(ValueError):
            receive_page(conn, fixtures.TOPIC, payload, at='2026-11-17T10:01:00Z')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_pages').fetchone()[0], 0)
        conn.close()

    def test_restart_keeps_watermark_gaps_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'journal.sqlite3'
            conn = connect(path)
            ensure_topic(conn, fixtures.TOPIC, fixtures.HUB, fixtures.SECRET, AT)
            register_lease(conn, fixtures.TOPIC, fixtures.HUB, 'lease-21', '2026-11-17T11:00:00Z', AT)
            receive_page(conn, fixtures.TOPIC, fixtures.page(10, 20, [ev('a', 'r2', 12)]),
                         at='2026-11-17T10:01:00Z')
            before = coverage(conn, fixtures.TOPIC)
            conn.close()

            conn = connect(path)
            self.assertEqual(coverage(conn, fixtures.TOPIC), before)
            self.assertEqual(before['gaps'], [{'cursor_start': 0, 'cursor_end': 10}])
            receive_page(conn, fixtures.TOPIC, fixtures.page(0, 10, [ev('a', 'r1', 3)]),
                         at='2026-11-17T10:02:00Z')
            self.assertEqual(coverage(conn, fixtures.TOPIC)['watermark'], 20)
            self.assertEqual(article_states(conn, fixtures.TOPIC)[0]['current_revision'], 'r2')
            conn.close()
