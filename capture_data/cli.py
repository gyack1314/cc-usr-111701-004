"""查看冻结资料，或运维已持久化的订阅、回调与回补账本。"""
import argparse
import json
import os
from pathlib import Path

from . import report as report_module
from .callback import receive as receive_callback
from .domain import summarize
from .io import load
from .journal import receive as receive_manifest
from .lease import ensure_topic, register_lease, retire_lease, set_hub
from .projection import overview
from .source import receive_page
from .store import connect


def _read_body(path):
    return Path(path).read_bytes()


def _inspect(args):
    path = Path(args.file)
    manifest, digest = load(path)
    summary = summarize(manifest, path.parent)
    conn = connect(':memory:')
    source = receive_manifest(conn, manifest, path.parent)
    payload = {'manifest_sha256': digest, 'source_sha256': source, 'summary': summary,
               'overview': overview(conn, manifest['collection'])}
    conn.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _secret(args):
    secret = os.environ.get(args.secret_env, '') if args.secret_env else args.secret
    if not secret:
        raise SystemExit('需要 --secret 或有效的 --secret-env')
    return secret


def _subscribe(args):
    conn = connect(args.db)
    ensure_topic(conn, args.topic, args.hub, _secret(args), args.at)
    print(json.dumps(report_module.build(conn, args.topic), ensure_ascii=False, indent=2))
    conn.close()


def _hub_change(args):
    conn = connect(args.db)
    set_hub(conn, args.topic, args.hub, _secret(args), args.at)
    print(json.dumps(report_module.build(conn, args.topic), ensure_ascii=False, indent=2))
    conn.close()


def _lease(args):
    conn = connect(args.db)
    outcome = register_lease(conn, args.topic, args.hub, args.lease_id, args.expires, args.at)
    print(json.dumps({'lease': outcome, 'report': report_module.build(conn, args.topic)},
                     ensure_ascii=False, indent=2))
    conn.close()


def _retire(args):
    conn = connect(args.db)
    outcome = retire_lease(conn, args.topic, args.generation, args.at)
    print(json.dumps({'retired': outcome, 'report': report_module.build(conn, args.topic)},
                     ensure_ascii=False, indent=2))
    conn.close()


def _callback(args):
    conn = connect(args.db)
    headers = {}
    if args.signature:
        headers['x-hub-signature-256'] = args.signature
    if args.digest:
        headers['digest'] = args.digest
    outcome = receive_callback(conn, args.topic, _read_body(args.body), headers=headers, at=args.at)
    print(json.dumps({'callback': outcome, 'report': report_module.build(conn, args.topic)},
                     ensure_ascii=False, indent=2))
    conn.close()


def _page(args):
    conn = connect(args.db)
    outcome = receive_page(conn, args.topic, _read_body(args.body), at=args.at)
    print(json.dumps({'page': outcome, 'report': report_module.build(conn, args.topic)},
                     ensure_ascii=False, indent=2))
    conn.close()


def _report(args):
    conn = connect(args.db)
    print(json.dumps(report_module.build(conn, args.topic), ensure_ascii=False, indent=2))
    conn.close()


def _guard(handler):
    """把账本校验错误转成简洁的退出信息，保留非零退出码。"""
    def run(args):
        try:
            handler(args)
        except ValueError as exc:
            raise SystemExit('拒绝接收: ' + str(exc))
    return run


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)

    inspect_parser = commands.add_parser('inspect')
    inspect_parser.add_argument('file')
    inspect_parser.set_defaults(handler=_guard(_inspect))

    def common(subparser, topic_required=True):
        subparser.add_argument('--db', required=True)
        subparser.add_argument('--topic', required=topic_required)
        subparser.add_argument('--at')

    subscribe_parser = commands.add_parser('subscribe')
    common(subscribe_parser)
    subscribe_parser.add_argument('--hub', required=True)
    subscribe_parser.add_argument('--secret')
    subscribe_parser.add_argument('--secret-env')
    subscribe_parser.set_defaults(handler=_guard(_subscribe))

    hub_parser = commands.add_parser('set-hub')
    common(hub_parser)
    hub_parser.add_argument('--hub', required=True)
    hub_parser.add_argument('--secret')
    hub_parser.add_argument('--secret-env')
    hub_parser.set_defaults(handler=_guard(_hub_change))

    lease_parser = commands.add_parser('lease')
    common(lease_parser)
    lease_parser.add_argument('--hub', required=True)
    lease_parser.add_argument('--lease-id', required=True)
    lease_parser.add_argument('--expires', required=True)
    lease_parser.set_defaults(handler=_guard(_lease))

    retire_parser = commands.add_parser('retire')
    common(retire_parser)
    retire_parser.add_argument('--generation', type=int, required=True)
    retire_parser.set_defaults(handler=_guard(_retire))

    callback_parser = commands.add_parser('callback')
    common(callback_parser)
    callback_parser.add_argument('--body', required=True)
    callback_parser.add_argument('--signature')
    callback_parser.add_argument('--digest')
    callback_parser.set_defaults(handler=_guard(_callback))

    page_parser = commands.add_parser('page')
    common(page_parser)
    page_parser.add_argument('--body', required=True)
    page_parser.set_defaults(handler=_guard(_page))

    report_parser = commands.add_parser('report')
    common(report_parser)
    report_parser.set_defaults(handler=_guard(_report))

    args = parser.parse_args()
    args.handler(args)


if __name__ == '__main__':
    main()
