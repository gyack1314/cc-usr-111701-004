"""采集服务持久化入口：在原有冻结资料账本上增加订阅、租约与回补证据表。"""
from .journal import connect as _connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions(
 topic TEXT PRIMARY KEY,
 hub TEXT NOT NULL,
 secret TEXT NOT NULL,
 active_generation INTEGER NOT NULL DEFAULT 0,
 watermark INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases(
 topic TEXT NOT NULL,
 generation INTEGER NOT NULL,
 hub TEXT NOT NULL,
 lease_id TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 state TEXT NOT NULL,
 superseded_by INTEGER,
 recorded_at TEXT NOT NULL,
 PRIMARY KEY(topic,generation),
 UNIQUE(topic,hub,lease_id),
 FOREIGN KEY(topic) REFERENCES subscriptions(topic)
);

CREATE TABLE IF NOT EXISTS callbacks(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 topic TEXT NOT NULL,
 declared_topic TEXT,
 hub TEXT,
 lease_id TEXT,
 generation INTEGER,
 body_digest TEXT NOT NULL,
 body BLOB NOT NULL,
 headers TEXT NOT NULL,
 signature TEXT,
 accepted INTEGER NOT NULL,
 last_accepted INTEGER NOT NULL,
 checks TEXT NOT NULL,
 reasons TEXT NOT NULL,
 overlap INTEGER NOT NULL DEFAULT 0,
 deliveries INTEGER NOT NULL DEFAULT 1,
 first_seen_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL,
 UNIQUE(topic,body_digest)
);

CREATE TABLE IF NOT EXISTS notifications(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 topic TEXT NOT NULL,
 article_id TEXT NOT NULL,
 revision TEXT NOT NULL,
 source_url TEXT NOT NULL DEFAULT '',
 deleted INTEGER NOT NULL,
 cursor INTEGER,
 first_generation INTEGER,
 first_callback_id INTEGER,
 first_seen_at TEXT NOT NULL,
 refetch_state TEXT NOT NULL DEFAULT 'pending'
);

CREATE UNIQUE INDEX IF NOT EXISTS notifications_identity
 ON notifications(topic,article_id,revision,source_url,deleted);

CREATE TABLE IF NOT EXISTS notification_receipts(
 notification_id INTEGER NOT NULL,
 callback_id INTEGER NOT NULL,
 generation INTEGER,
 cursor INTEGER,
 delivered_at TEXT NOT NULL,
 PRIMARY KEY(notification_id,callback_id,generation),
 FOREIGN KEY(notification_id) REFERENCES notifications(id),
 FOREIGN KEY(callback_id) REFERENCES callbacks(id)
);

CREATE TABLE IF NOT EXISTS source_pages(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 topic TEXT NOT NULL,
 declared_topic TEXT,
 cursor_start INTEGER NOT NULL DEFAULT 0,
 cursor_end INTEGER NOT NULL DEFAULT 0,
 complete INTEGER NOT NULL DEFAULT 0,
 end_of_feed INTEGER NOT NULL DEFAULT 0,
 next_cursor INTEGER,
 digest TEXT NOT NULL,
 raw BLOB NOT NULL,
 accepted INTEGER NOT NULL,
 rejection TEXT,
 received_at TEXT NOT NULL,
 UNIQUE(topic,digest),
 FOREIGN KEY(topic) REFERENCES subscriptions(topic)
);

CREATE INDEX IF NOT EXISTS source_pages_range
 ON source_pages(topic,accepted,complete,cursor_start,cursor_end);

CREATE TABLE IF NOT EXISTS source_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 topic TEXT NOT NULL,
 article_id TEXT NOT NULL,
 revision TEXT NOT NULL,
 source_url TEXT NOT NULL DEFAULT '',
 deleted INTEGER NOT NULL,
 event_cursor INTEGER NOT NULL,
 page_digest TEXT NOT NULL,
 event_digest TEXT NOT NULL,
 observed_at TEXT NOT NULL,
 UNIQUE(topic,event_digest),
 FOREIGN KEY(topic) REFERENCES subscriptions(topic)
);

CREATE INDEX IF NOT EXISTS source_events_cursor
 ON source_events(topic,event_cursor);
"""


def connect(path):
    """打开账本并补齐订阅/回补表；原有 sources、records 行为不变。"""
    c = _connect(path)
    c.executescript(SCHEMA)
    return c
