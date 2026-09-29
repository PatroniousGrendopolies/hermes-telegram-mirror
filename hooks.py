"""Hot-path hook ingress. Callbacks never perform SQLite, formatting or network I/O."""
from collections import deque
from os import getpid
import atexit
import logging
import threading
import time


class HookBridge:
    NAMES = {
        'pre_llm_call': 'start', 'on_stream_start': 'stream',
        'on_interim_message': 'interim', 'pre_tool_call': 'tool_start',
        'post_tool_call': 'tool_end', 'post_llm_call': 'final',
        'on_session_end': 'stop', 'on_session_finalize': 'stop',
        'agent_loop_stopped': 'stop',
    }

    def __init__(self, runtime, capacity=256):
        self.runtime = runtime
        self.capacity = capacity
        self.events = deque(maxlen=capacity)
        # Reserved lossless completion lane, normally zero/one for a serial Bot Chat.
        # Finals can exceed the transient cap; a finite nonblocking lossless queue is impossible.
        self.finals = deque()

        self.wake = threading.Event()
        self.done = threading.Event()
        self.count = self.dropped = self.max_ns = 0
        self.session_id = runtime.delivery.session()
        self.thread = threading.Thread(target=self.run, name='telegram-mirror-persist', daemon=True)
        self.thread.start()
        atexit.register(self.close)

    def callback(self, kind):
        def capture(**payload):
            started = time.perf_counter_ns()
            sid = payload.get('session_id') or payload.get('session_key')
            # Do not copy histories, results, error bodies or arbitrary argument dictionaries.
            event = {'kind': kind, 'session_id': sid, 'turn_id': payload.get('turn_id'),
                     'at': time.time()}
            if kind in {'start', 'final'}:
                event['user_message'] = payload.get('user_message', '')
            if kind == 'final':
                event['assistant_response'] = payload.get('assistant_response', '')
            elif kind == 'interim':
                event['text'] = payload.get('text', '')
                event['iteration'] = payload.get('iteration', 0)
            elif kind in {'tool_start', 'tool_end'}:
                args = payload.get('args') or {}
                primary = next((args[k] for k in ('command', 'query', 'path', 'url', 'text', 'pattern', 'name')
                                if isinstance(args.get(k), str)), '')
                event.update(tool_name=payload.get('tool_name', 'tool'), primary=primary[:8192],
                             tool_call_id=payload.get('tool_call_id'),
                             status=payload.get('status'), duration_ms=payload.get('duration_ms'))
            # CPython deque append/popleft are atomic. No contended mutex on a hook.
            if kind in {'final', 'stop'}:
                self.finals.append(event)
            else:
                if len(self.events) >= self.capacity:
                    self.dropped += 1
                self.events.append(event)
            # Worker polls at 50ms; even Event.set() would take a mutex on this path.
            elapsed = time.perf_counter_ns() - started
            self.count += 1
            self.max_ns = max(self.max_ns, elapsed)
            # Explicitly None: never modify/block pre_tool_call or alter pre_llm_call context.
            return None
        return capture

    def pop(self):
        if self.events and self.finals:
            return (self.events if self.events[0]['at'] <= self.finals[0]['at'] else self.finals).popleft()
        if self.events:
            return self.events.popleft()
        if self.finals:
            return self.finals.popleft()
        return None

    def run(self):
        refreshed = 0
        while True:
            event = self.pop()
            if event is None:
                if self.done.is_set():
                    return
                self.wake.wait(0.05)
                self.wake.clear()
                continue
            try:
                # Resolve cached canonical ID off-path; compression is handled on a cache miss.
                if event['session_id'] != self.session_id or time.monotonic() - refreshed > 10:
                    self.session_id = self.runtime.delivery.session()
                    refreshed = time.monotonic()
                if event['session_id'] != self.session_id:
                    continue
                self.runtime.progress.handle(event)
                if event['kind'] == 'final':
                    stats = {'pid': getpid(), 'version': 2,
                             'callbacks': self.count, 'max_callback_us': self.max_ns / 1000,
                             'dropped_progress': self.dropped}
                    self.runtime.store.set('hook_metrics', stats)
                    logging.getLogger('telegram-mirror').info('hook enqueue max_us=%.1f count=%s', stats['max_callback_us'], self.count)
            except Exception:
                logging.getLogger('telegram-mirror').warning('mirror persistence event failed; no payload logged')
                if event['kind'] == 'final':
                    self.finals.appendleft(event)
                    time.sleep(0.2)

    def close(self):
        self.done.set()
        self.wake.set()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=5)
