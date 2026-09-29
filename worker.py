"""Dedicated mirror process: never starts a Hermes gateway, cron or kanban service."""
import argparse
import importlib.util
import logging
import os
from pathlib import Path
import signal
import sys


def load_package():
    root = Path(__file__).resolve().parent
    name = '_telegram_mirror_worker'
    spec = importlib.util.spec_from_file_location(name, root / '__init__.py',
                                                submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--profile', required=True)
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    # Set before importing Hermes compatibility modules. Do NOT load any .env.
    os.environ['HERMES_HOME'] = str(home)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ.pop('TELEGRAM_BOT_TOKEN', None)
    sys.dont_write_bytecode = True
    name = load_package()
    settings = importlib.import_module(name + '.configuration').load_settings(home, args.profile)
    if settings.get('host') != 'standalone':
        raise ValueError('Standalone worker requires host: standalone')
    Mirror = importlib.import_module(name + '.mirror').Mirror
    runtime = Mirror(home, settings)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: runtime.stop.set())
    runtime._lead(standalone=True)
    if runtime.store.get('startup_error') or runtime.store.get('poll_error'):
        return 1
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        sys.exit(main())
    except Exception:
        logging.error('Mirror worker failed; details suppressed. Inspect local configuration and receipts.')
        sys.exit(1)
