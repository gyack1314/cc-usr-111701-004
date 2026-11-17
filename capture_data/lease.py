"""租约代际：续租交叠时旧、新回调各自归属，不互相覆盖。"""
from datetime import datetime, timezone


def utc(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('时间戳缺失')
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def ensure_topic(conn, topic, hub, secret, at):
    """登记订阅身份；主题、hub 与密钥是回调核验的前提。"""
    if not topic or not hub or not secret:
        raise ValueError('订阅身份不完整')
    moment = iso(utc(at))
    with conn:
        conn.execute('INSERT OR IGNORE INTO subscriptions VALUES(?,?,?,0,0,?)', (topic, hub, secret, moment))
    return conn.execute('SELECT topic,hub,secret,active_generation,watermark FROM subscriptions WHERE topic=?', (topic,)).fetchone()


def set_hub(conn, topic, hub, secret, at):
    """换 hub 或轮换密钥；已有代际与证据保持不变。"""
    if not hub or not secret:
        raise ValueError('新的 hub 或密钥不完整')
    moment = iso(utc(at))
    with conn:
        updated = conn.execute('UPDATE subscriptions SET hub=?,secret=?,updated_at=? WHERE topic=?', (hub, secret, moment, topic)).rowcount
        if not updated:
            raise ValueError('未知订阅')
    return conn.execute('SELECT topic,hub,secret,active_generation,watermark FROM subscriptions WHERE topic=?', (topic,)).fetchone()


def register_lease(conn, topic, hub, lease_id, expires_at, at):
    """登记一条租约；同 hub 同 id 视为重送，新租约分配更大代际。

    旧租约在到期前仍被标记为 overlapping，其回调继续按旧代际接收，
    因此续租交叠期间不会把重复通知误记为新稿。
    """
    moment = utc(at)
    expiry = utc(expires_at)
    if expiry <= moment:
        raise ValueError('租约登记即已过期')
    if not lease_id:
        raise ValueError('租约标识缺失')
    with conn:
        row = conn.execute('SELECT generation,hub,lease_id,expires_at,state FROM leases WHERE topic=? AND hub=? AND lease_id=?',
                           (topic, hub, lease_id)).fetchone()
        if row:
            generation, state = row[0], row[4]
            # 重送只更新到期时间，不复活已停用或已被接替的代际
            conn.execute('UPDATE leases SET expires_at=? WHERE topic=? AND generation=?', (iso(expiry), topic, generation))
            conn.execute('UPDATE subscriptions SET updated_at=? WHERE topic=?', (iso(moment), topic))
            return {'generation': generation, 'created': False, 'overlap': state == 'overlapping',
                    'state': state}
        current = conn.execute('SELECT generation,hub,lease_id,expires_at,state FROM leases WHERE topic=? ORDER BY generation DESC', (topic,)).fetchall()
        generation = (current[0][0] + 1) if current else 1
        overlapping = [r[0] for r in current if r[4] in ('active', 'overlapping') and utc(r[3]) > moment]
        conn.execute('INSERT INTO leases VALUES(?,?,?,?,?,?,?,?)',
                     (topic, generation, hub, lease_id, iso(expiry), 'active', None, iso(moment)))
        for old_generation in overlapping:
            conn.execute('UPDATE leases SET state=?,superseded_by=? WHERE topic=? AND generation=?',
                         ('overlapping', generation, topic, old_generation))
        conn.execute('UPDATE subscriptions SET active_generation=?,updated_at=? WHERE topic=?', (generation, iso(moment), topic))
    return {'generation': generation, 'created': True, 'overlap': bool(overlapping), 'overlapping_generations': overlapping}


def lease_context(conn, topic, hub, lease_id, at, declared_expiry=None):
    """判定回调所属代际；未知、过期、主题不符的租约不能作为新通知入账。

    回调声明的有效期不得超出已登记租约，否则按无效租约拒收。
    """
    moment = utc(at)
    row = conn.execute('SELECT generation,hub,lease_id,expires_at,state,superseded_by FROM leases WHERE topic=? AND hub=? AND lease_id=?',
                       (topic, hub, lease_id)).fetchone()
    if not row:
        return {'usable': False, 'generation': None, 'reason': '未知租约'}
    generation, recorded_hub, _, expires_at, state, superseded_by = row
    expired = utc(expires_at) <= moment
    reason = None
    if expired:
        reason = '租约已过期'
    elif state == 'retired':
        reason = '租约已停用'
    elif declared_expiry is not None and utc(declared_expiry) > utc(expires_at):
        reason = '回调声明有效期超出登记租约'
    return {'usable': reason is None, 'generation': generation, 'reason': reason,
            'expires_at': expires_at, 'state': state, 'superseded_by': superseded_by}


def retire_lease(conn, topic, generation, at):
    """显式停用某代际；已接收的证据与文章状态不受影响。"""
    with conn:
        updated = conn.execute('UPDATE leases SET state=? WHERE topic=? AND generation=?', ('retired', topic, generation)).rowcount
        if not updated:
            raise ValueError('未知租约代际')
    return {'topic': topic, 'generation': generation, 'state': 'retired', 'at': iso(utc(at))}
