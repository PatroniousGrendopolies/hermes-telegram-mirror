"""Measure the real hook callbacks under a synthetic burst, without any network."""
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('mirror_benchmark', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pkg
spec.loader.exec_module(pkg)
Bridge = importlib.import_module('mirror_benchmark.hooks').HookBridge


def main():
    runtime = SimpleNamespace(delivery=SimpleNamespace(session=lambda: 'canonical'),
                              progress=SimpleNamespace(handle=lambda event: None), store=Mock())
    bridge = Bridge(runtime)
    callbacks = [bridge.callback(kind) for kind in ['start', 'stream', 'tool_start', 'tool_end', 'interim', 'final']]
    samples = []
    for i in range(10000):
        begin = time.perf_counter_ns()
        callbacks[i % len(callbacks)](session_id='canonical', turn_id=str(i // 6),
                                     tool_call_id=str(i), tool_name='terminal', args={'command': 'date'},
                                     status='success', duration_ms=1, user_message='benchmark',
                                     text='working', assistant_response='done')
        samples.append((time.perf_counter_ns() - begin) / 1000)
    bridge.close()
    ordered = sorted(samples)
    print(json.dumps({'calls': len(samples), 'median_us': statistics.median(samples),
                      'p95_us': ordered[int(len(ordered) * .95)], 'max_us': max(samples),
                      'callback_internal_max_us': bridge.max_ns / 1000,
                      'dropped_progress_under_burst': bridge.dropped, 'network_calls': 0}))


if __name__ == '__main__':
    main()
