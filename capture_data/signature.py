"""核对 WebSub 回调的原始请求签名。"""
import hashlib
import hmac


def parse(raw):
    """从请求头取出声明算法与十六进制摘要；缺失或格式非法时返回 None。"""
    if raw is None:
        return None
    text = raw.strip()
    if text.lower().startswith('sha256='):
        algorithm, value = 'sha256', text.split('=', 1)[1]
    elif text.lower().startswith('sha1='):
        algorithm, value = 'sha1', text.split('=', 1)[1]
    else:
        return None
    if len(value) != (64 if algorithm == 'sha256' else 40):
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return algorithm, value.lower()


def verify(raw, body, secret):
    """按声明算法对原始字节计算 HMAC；不合法、算法不支持或密钥为空都视为未通过。"""
    if not secret:
        return {'valid': False, 'algorithm': None, 'reason': '缺少订阅密钥'}
    parsed = parse(raw)
    if parsed is None:
        return {'valid': False, 'algorithm': None, 'reason': '签名格式非法或不支持'}
    algorithm, expected = parsed
    digest = hmac.new(secret.encode('utf-8'), body, getattr(hashlib, algorithm)).hexdigest()
    if not hmac.compare_digest(digest, expected):
        return {'valid': False, 'algorithm': algorithm, 'reason': '签名与请求原文不符'}
    return {'valid': True, 'algorithm': algorithm, 'reason': None}
