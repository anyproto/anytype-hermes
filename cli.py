"""Operator commands expose recovery state without showing stored message bodies."""

import asyncio
import json
import sqlite3

from hermes_constants import get_hermes_home


def setup_parser(parser):
    commands = parser.add_subparsers(dest='anytype_command', required=True)
    commands.add_parser('status', help='Show local chat health and receipt counts')
    reconcile = commands.add_parser('reconcile', help='Reconcile history while the gateway is stopped')
    reconcile.add_argument('--deep', action='store_true', help='Scan up to 100,000 retained messages per chat')


def local_status(path):
    if not path.exists():
        return {'status': 'not_initialized'}
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return {
            'chats': [dict(row) for row in db.execute('SELECT target,health FROM chats')],
            'inputs': {row[0]: row[1] for row in db.execute('SELECT status,count(*) FROM inputs GROUP BY status')},
            'deliveries': {row[0]: row[1] for row in db.execute('SELECT status,count(*) FROM deliveries GROUP BY status')},
            'uncertain_operations': [dict(row) for row in db.execute(
                "SELECT operation,chunk,target,status FROM deliveries WHERE status IN ('uncertain','failed','sending')")],
        }


async def reconcile_history(deep):
    from gateway.config import Platform, load_gateway_config
    from .adapter import AnytypeAdapter
    config = load_gateway_config().platforms.get(Platform('anytype'))
    if config is None:
        raise ValueError('Configure the Anytype platform first')
    adapter = AnytypeAdapter(config)
    try:
        if not await adapter.connect(receive=False):
            raise RuntimeError('Could not acquire Anytype account; stop its gateway before reconciliation')
        for target in adapter.settings.targets:
            await adapter.reconcile(target, deep=deep)
        return adapter.store.summary()
    finally:
        await adapter.disconnect()


def dispatch(args):
    if args.anytype_command == 'status':
        result = local_status(get_hermes_home() / 'anytype' / 'state.sqlite3')
    else:
        result = asyncio.run(reconcile_history(args.deep))
    print(json.dumps(result, indent=2))
    return 0
