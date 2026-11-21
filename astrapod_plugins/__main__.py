"""List, configure or run the AstraPod plugin host."""
import argparse
from dataclasses import asdict
import json
import logging
import signal
import threading

from .host import PluginHost


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='plugin configuration file')
    subparsers = parser.add_subparsers(dest='command', required=True)
    listing = subparsers.add_parser('list', help='list discovered plugins and configuration')
    listing.add_argument('--json', action='store_true', help='print JSON')
    for name in ('enable', 'disable'):
        subparsers.add_parser(name, help=f'{name} a plugin').add_argument('id')
    run = subparsers.add_parser('run', help='supervise enabled plugins')
    run.add_argument('--events', action='store_true', help='print normalized events as JSON lines')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    host = PluginHost(args.config)
    try:
        if args.command == 'list':
            rows = host.list()
            if args.json:
                print(json.dumps([asdict(row) for row in rows], indent=2))
            else:
                for row in rows:
                    print(f"{row.id}\t{'enabled' if row.enabled else 'disabled'}\t{row.name}")
        elif args.command in ('enable', 'disable'):
            host.enable(args.id, args.command == 'enable')
        else:
            return supervise(host, args.events)
    except KeyError as exc:
        parser.error(f'unknown plugin: {exc.args[0]}')
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


def supervise(host, events):
    stop = threading.Event()

    def finish(signum, frame):
        stop.set()

    def report(record):
        if record['type'] == 'status':
            logging.info('%s: %s %s', record['source'], record['state'], record['detail'])
        if events:
            print(json.dumps(record), flush=True)

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    host.subscribe(report)
    host.start()
    try:
        while not stop.wait(.25):
            if not host.running:
                logging.error('Plugin supervisor stopped unexpectedly')
                return 1
    finally:
        host.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
