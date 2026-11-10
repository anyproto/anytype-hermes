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
    for name, help_text in [('setup', 'Prepare, resume and publish an Anytype connection'),
                            ('join', 'Join or refresh a space and reconcile discovery scope')]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--mode', choices=['cli', 'desktop', 'external'])
        command.add_argument('--resume', action='store_true', help='Require an existing setup record')
        command.add_argument('--non-interactive', action='store_true', help='Never prompt; require explicit choices')
        command.add_argument('--input', help='Versioned non-secret JSON choices and private secret references')
        command.add_argument('--json', action='store_true', help='Print the stable automation result envelope')
        if name == 'setup':
            command.add_argument('--cancel', action='store_true', help='Abandon an uncommitted candidate safely')
            command.add_argument('--rollback', action='store_true', help='Explicitly restore prior settings under the publication barrier')
    doctor = commands.add_parser('doctor', help='Read-only live API, policy, home and MCP checks')
    doctor.add_argument('--json', action='store_true', help='Print the stable automation result envelope')


def local_status(path):
    if not path.exists():
        return {'status': 'not_initialized'}
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        result = {
            'chats': [dict(row) for row in db.execute('SELECT target,health FROM chats')],
            'inputs': {row[0]: row[1] for row in db.execute('SELECT status,count(*) FROM inputs GROUP BY status')},
            'deliveries': {row[0]: row[1] for row in db.execute('SELECT status,count(*) FROM deliveries GROUP BY status')},
            'uncertain_operations': [dict(row) for row in db.execute(
                "SELECT operation,chunk,target,status FROM deliveries WHERE status IN ('uncertain','failed','sending')")],
        }
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_spaces'").fetchone():
            result['spaces'] = [dict(row) for row in db.execute('SELECT space_id,health FROM discovery_spaces')]
        return result


async def reconcile_history(deep):
    from gateway.config import Platform, load_gateway_config
    from .adapter import AnytypeAdapter
    from .config import Target
    config = load_gateway_config().platforms.get(Platform('anytype'))
    if config is None:
        raise ValueError('Configure the Anytype platform first')
    adapter = AnytypeAdapter(config)
    try:
        if not await adapter.connect(receive=False):
            raise RuntimeError('Could not acquire Anytype account; stop its gateway before reconciliation')
        targets = ([Target.parse(row['target'])
                    for row in adapter.store.catalog() if row['available'] and row['space_id'] in adapter._spaces]
                   if adapter.discovery else adapter.settings.targets)
        for target in targets:
            await adapter.reconcile(target, deep=deep)
        return adapter.store.summary()
    finally:
        await adapter.disconnect()


def dispatch(args):
    if args.anytype_command == 'status':
        result = local_status(get_hermes_home() / 'anytype' / 'state.sqlite3')
    elif args.anytype_command == 'reconcile':
        result = asyncio.run(reconcile_history(args.deep))
    else:
        from .onboarding import Wizard, load_input
        from .setup_store import SetupError, outcome, exit_code
        try:
            if args.anytype_command == 'doctor':
                from .diagnostics import doctor
                from hermes_cli.config import load_config_readonly
                result = asyncio.run(doctor(load_config_readonly()))
            else:
                choices = load_input(args.input) if args.input else {}
                if args.mode:
                    choices['mode'] = args.mode
                wizard = Wizard(get_hermes_home(), interactive=not args.non_interactive)
                if args.anytype_command == 'join':
                    result = asyncio.run(wizard.join(choices, resume=args.resume))
                else:
                    result = asyncio.run(wizard.run(choices, resume=args.resume, cancel=args.cancel, rollback=args.rollback))
        except SetupError as error:
            result = outcome(error.state, error=error, next_action=error.next_action)
        except Exception:
            error = SetupError('configuration', 'Unable to safely inspect the profile configuration.', state='error')
            result = outcome('error', error=error, next_action='Repair the profile configuration, then retry.')
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return exit_code(result)
    print(json.dumps(result, indent=2))
    return 0
