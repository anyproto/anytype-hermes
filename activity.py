"""Ephemeral chat activity: one execution ID, serial updates, explicit terminal status."""

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx

from .client import APIError

log = logging.getLogger(__name__)
_SECRET_KEY = re.compile(r'(^|_)(password|secret|token|api_key|authorization|cookie|credential)(_|$)', re.I)
_TOKEN = re.compile(r'anytype_[a-z0-9]{30,}_[a-z0-9]+', re.I)


def clean(value, secrets=(), depth=0):
    """Keep JSON values (including falsy results), with bounded depth and secret redaction."""
    if isinstance(value, str):
        from agent.redact import redact_for_egress
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[redacted]')
        return redact_for_egress(_TOKEN.sub('[redacted]', value))
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if abs(value) < float('inf') else str(value)
    if depth >= 12:
        return '[nested value omitted]'
    if isinstance(value, dict):
        return {str(k): '[redacted]' if _SECRET_KEY.search(str(k)) else clean(v, secrets, depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, secrets, depth + 1) for v in value]
    return clean(str(value), secrets, depth + 1)


def encode_payload(payload):
    # Match the extra escaping Heart's Go JSON encoder uses when checking pubsub's byte limit.
    return (json.dumps(payload, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
            .replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')).encode()


def bounded_payload(text, data=None):
    payload = {'text': str(text or 'Thinking about the next step')[:240]}
    if data:
        payload['data'] = data
    if len(encode_payload(payload)) <= 60_000:
        return payload
    # Keep identity/state intact and make truncation visible in the expanded result.
    kept = {k: data[k] for k in ('tool_call_id', 'tool_name', 'status') if k in data}
    if 'result' in data:
        kept['result'] = {'truncated': True, 'preview': encode_payload(data['result']).decode()[:6000]}
    kept['metadata_truncated'] = True
    return {'text': payload['text'], 'data': kept}


@dataclass
class Call:
    target: object
    run_id: str
    payload: dict
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_at: float | None = None
    revision: int = 0


class ActivityRun:
    """Worker-thread callbacks bound to one immutable chat and run, never a global current chat."""

    def __init__(self, publisher, target):
        self.publisher, self.target = publisher, target
        self.id = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._calls = {}
        self._closed = False

    def start(self, call_id, name, args, *, label=None):
        with self._lock:
            if self._closed:
                return
            key = str(call_id or '')
            previous = self._calls.get(key)
            if previous and not previous[1]:
                self.publisher.submit(self.target, self.id, previous[0], str(name), {},
                                      'error', 'Execution superseded by another start')
            execution = f'run_{self.id}_call_{uuid.uuid4().hex}'
            self._calls[key] = (execution, False)
            self.publisher.submit(self.target, self.id, execution, str(name), args, 'in_progress', label=label)

    def complete(self, call_id, name, args, result, *, status=None):
        with self._lock:
            if self._closed:
                return
            key = str(call_id or '')
            previous = self._calls.get(key)
            if previous and previous[1]:
                return  # duplicate completion cannot overwrite a terminal result
            execution = previous[0] if previous else f'run_{self.id}_call_{uuid.uuid4().hex}'
            self._calls[key] = (execution, True)
            from agent.display import _detect_tool_failure
            failed, _ = _detect_tool_failure(str(name), result)
            failed = failed or status in {'error', 'failed', 'blocked', 'cancelled', 'timeout'}
            parsed = result
            if isinstance(parsed, str):
                try:
                    parsed = json.loads(parsed)
                except ValueError:
                    pass
            if isinstance(parsed, dict):
                failed = failed or (parsed.get('isError') is True or parsed.get('is_error') is True
                                    or parsed.get('success') is False
                                    or parsed.get('status') in {'error', 'failed'})
            self.publisher.submit(self.target, self.id, execution, str(name), args,
                                  'error' if failed else 'complete', result)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.publisher.loop.call_soon_threadsafe(self.publisher.end_run, self.id)


_MISSING = object()


class ActivityPublisher:
    def __init__(self, client, *, secrets=(), interval=2.0, terminal_retry_seconds=30.0):
        self.client, self.secrets = client, secrets
        self.loop = asyncio.get_running_loop()
        self.interval, self.terminal_retry_seconds = interval, terminal_retry_seconds
        self.calls, self.tasks, self.labels = {}, set(), {}
        self.typing_started = {}
        self.closed = False
        self.unavailable_until = 0.0

    def run(self, target):
        return ActivityRun(self, target)

    def submit(self, target, run_id, execution, name, args, status, result=_MISSING, *, label=None):
        args = clean(args if isinstance(args, dict) else {}, self.secrets)
        data = {k: v for k, v in args.items() if k not in {'tool_call_id', 'tool_name', 'status', 'result'}}
        data.update(tool_call_id=execution, tool_name=name, status=status)
        if result is not _MISSING:
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    pass
            data['result'] = clean(result, self.secrets)
        from agent.display import build_status_phrase
        label = label or build_status_phrase(name, args) or f'Running {name}'
        if status != 'in_progress':
            label = f'{name}: {"complete" if status == "complete" else "failed"}'
        payload = bounded_payload(clean(label, self.secrets), data)
        self.loop.call_soon_threadsafe(self._update, target, run_id, execution, payload)

    def _update(self, target, run_id, execution, payload):
        if self.closed:
            return
        call = self.calls.get(execution)
        if call is not None and call.terminal_at is not None:
            return
        if call is None:
            call = Call(target, run_id, payload)
            self.calls[execution] = call
            task = asyncio.create_task(self._publish_call(execution, call))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        call.payload = payload
        call.revision += 1
        if payload['data']['status'] != 'in_progress':
            call.terminal_at = time.monotonic()
        call.changed.set()

    def set_text(self, target, text):
        if not self.closed:
            self.labels[target.key] = bounded_payload(clean(text, self.secrets))['text']
            task = asyncio.create_task(self._post(target, bounded_payload(self.labels[target.key])))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def typing(self, target, text=None):
        if self.closed:
            return
        started = self.typing_started.setdefault(target.key, time.monotonic())
        # Tool executions already refresh their own structured rows. A second generic
        # status would compete with them for the chat's activity label.
        if any(c.target == target and c.terminal_at is None for c in self.calls.values()):
            return
        label = text or self.labels.get(target.key)
        if not label:
            elapsed = int(time.monotonic() - started)
            minutes, seconds = divmod(elapsed, 60)
            label = ('Thinking about the next step' if elapsed < 30
                     else f'Working — {minutes}m{seconds:02d}s')
        await self._post(target, bounded_payload(clean(label, self.secrets)))

    def stop_typing(self, target):
        self.labels.pop(target.key, None)
        self.typing_started.pop(target.key, None)
        # Blank text means Typing. Stop refreshing and let the transient label expire.

    async def _post(self, target, payload):
        if time.monotonic() < self.unavailable_until:
            return False
        try:
            await self.client.publish_status(target, payload)
            return True
        except APIError as error:
            delay = error.retry_after if error.retry_after is not None else self.interval
            if error.status in {401, 403, 404}:
                delay = 30
            self.unavailable_until = time.monotonic() + max(self.interval, delay)
            log.warning('Anytype activity publication failed: HTTP %s; retry in %.1fs', error.status, delay)
        except (httpx.HTTPError, ValueError) as error:
            log.debug('Anytype activity publication failed: %s', type(error).__name__)
        return False

    async def _publish_call(self, execution, call):
        try:
            while True:
                call.changed.clear()
                revision, payload = call.revision, call.payload
                success = await self._post(call.target, payload)
                if revision != call.revision:
                    continue  # completion arrived during POST; serialize it after this request
                if call.terminal_at is not None:
                    if success:
                        return
                    if time.monotonic() - call.terminal_at >= self.terminal_retry_seconds:
                        log.warning('Anytype final activity status could not be published: %s', execution)
                        return
                try:
                    await asyncio.wait_for(call.changed.wait(), self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.calls.pop(execution, None)

    def end_run(self, run_id):
        for execution, call in list(self.calls.items()):
            if call.run_id == run_id and call.terminal_at is None:
                data = {**call.payload['data'], 'status': 'error',
                        'result': {'error': 'Execution ended without a tool result (interrupted or cancelled)'}}
                self._update(call.target, run_id, execution,
                             bounded_payload(f'{data["tool_name"]}: interrupted', data))
            if call.run_id == run_id:
                self.stop_typing(call.target)

    async def close(self):
        for run_id in {c.run_id for c in self.calls.values()}:
            self.end_run(run_id)
        self.closed = True
        if self.tasks:
            done, pending = await asyncio.wait(list(self.tasks), timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if pending:
                log.warning('Gateway closed before %d final activity publications were confirmed', len(pending))
