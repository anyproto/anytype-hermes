"""Bounded REST requests and an SSE parser that retains id-only checkpoints."""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx


class APIError(Exception):
    def __init__(self, status, retry_after=None):
        self.status = status
        self.retry_after = retry_after
        # Server bodies may contain personal data. Keep diagnostics structural.
        super().__init__(f'Anytype HTTP {status}')


class ProtocolError(Exception):
    pass


class ResyncRequired(ProtocolError):
    pass


@dataclass(frozen=True)
class Frame:
    event: str
    data: str
    id: str | None = None
    retry_ms: int | None = None


async def parse_sse(lines):
    event, data, cursor, retry, size = '', [], None, None, 0
    first = True
    async for line in lines:
        if first:
            line = line.removeprefix('\ufeff')
            first = False
        size += len(line)
        if size > 2_000_000:
            raise ProtocolError('SSE frame exceeds limit')
        if line == '':
            if data or cursor is not None or retry is not None:
                yield Frame(event or 'message', '\n'.join(data), cursor, retry)
            event, data, cursor, retry, size = '', [], None, None, 0
            continue
        if line.startswith(':'):
            continue
        field, sep, value = line.partition(':')
        if sep and value.startswith(' '):
            value = value[1:]
        if field == 'data':
            data.append(value)
        elif field == 'event':
            event = value
        elif field == 'id' and '\x00' not in value:
            cursor = value
        elif field == 'retry' and value.isascii() and value.isdigit() and len(value) < 12:
            retry = int(value)
    # An unterminated frame was never committed by the sender; resume it.


def retry_seconds(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None


class Client:
    def __init__(self, base_url, token, *, transport=None):
        if not token:
            raise ValueError('The selected Anytype API credential is required')
        self.http = httpx.AsyncClient(
            base_url=base_url, headers={'Authorization': f'Bearer {token}'},
            timeout=httpx.Timeout(30, connect=10), follow_redirects=False,
            trust_env=False, transport=transport,
        )
        self._write_lock = asyncio.Lock()
        self._last_write = 0.0
        self.target_allowed = None

    async def close(self):
        await self.http.aclose()

    @staticmethod
    def chat_path(target):
        return f'/v2/spaces/{quote(target.space_id, safe="")}/chats/{quote(target.chat_id, safe="")}/messages'

    async def request(self, method, path, *, body=None, key=None, params=None, allowed=None):
        headers = {'Idempotency-Key': key} if key else None
        if method != 'GET':
            async with self._write_lock:
                await asyncio.sleep(max(0, self._last_write + 1 - time.monotonic()))
                if allowed is not None and not allowed():
                    raise APIError(403)
                self._last_write = time.monotonic()
                response = await self.http.request(method, path, json=body, headers=headers, params=params)
        else:
            response = await self.http.get(path, params=params)
        if response.status_code >= 300:
            raise APIError(response.status_code, retry_seconds(response.headers.get('Retry-After')))
        try:
            result = response.json()
        except ValueError as error:
            raise ProtocolError('Invalid JSON response') from error
        if not isinstance(result, dict):
            raise ProtocolError('Expected a JSON object')
        return result

    async def rows(self, path, **params):
        offset = 0
        while True:
            page = await self.request('GET', path, params={**params, 'offset': offset, 'limit': 100})
            rows = page.get('data', [])
            if not isinstance(rows, list):
                raise ProtocolError('Invalid paginated response')
            for row in rows:
                yield row
            if not page.get('has_more', False):
                return
            if not rows:
                raise ProtocolError('Pagination made no progress')
            offset += len(rows)

    async def history(self, target, **params):
        return await self.request('GET', self.chat_path(target), params={'limit': 1000, **params})

    async def send(self, target, body, key):
        result = await self.request('POST', self.chat_path(target), body=body, key=key,
                                    allowed=lambda: self.target_allowed is None or self.target_allowed(target.key))
        if not isinstance(result.get('id'), str) or not result['id']:
            raise ProtocolError('Send response lacks a message ID')
        return result['id']

    async def publish_status(self, target, payload):
        from .activity import encode_payload
        encoded = encode_payload(payload)
        if len(encoded) > 65_508:
            raise ValueError('Activity payload exceeds the pubsub byte limit')
        if self.target_allowed is not None and not self.target_allowed(target.key):
            raise APIError(403)
        # Heart's limiter remains authoritative. Activity must not occupy the chat-send
        # pacing lock; independent calls need two-second refreshes. Do not deduplicate refreshes.
        response = await self.http.post(
            self.chat_path(target).removesuffix('/messages') + '/status', content=encoded,
            headers={'Content-Type': 'application/json'}, timeout=httpx.Timeout(2, connect=1),
        )
        if response.status_code != 200:
            raise APIError(response.status_code, retry_seconds(response.headers.get('Retry-After')))

    async def stream(self, target, cursor):
        headers = {'Last-Event-ID': cursor} if cursor else {}
        async with self.http.stream(
            'GET', self.chat_path(target) + '/stream', headers=headers,
            params={'limit': 1000, 'heartbeat': 30}, timeout=httpx.Timeout(75, connect=10),
        ) as response:
            if response.status_code != 200:
                raise APIError(response.status_code, retry_seconds(response.headers.get('Retry-After')))
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                raise ProtocolError('Expected an SSE stream')
            async for frame in parse_sse(response.aiter_lines()):
                yield frame

    async def subscription(self, space, *, search=None):
        """Heart subscriptions reconnect with snapshots, never Last-Event-ID.

        POST search is a read and must not hold the mutation pacing lock.
        """
        path = f'/v2/spaces/{quote(space, safe="")}'
        params = {'heartbeat': 30, 'ids': 'full'}
        if search is None:
            path += '/chats/stream'
            params['include'] = 'discussions'
        else:
            path += '/search/stream'
        async with self.http.stream(
            'GET' if search is None else 'POST', path, json=search, params=params,
            timeout=httpx.Timeout(75, connect=10),
        ) as response:
            if response.status_code != 200:
                raise APIError(response.status_code, retry_seconds(response.headers.get('Retry-After')))
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                raise ProtocolError('Expected an SSE subscription')
            async for frame in parse_sse(response.aiter_lines()):
                if frame.id is not None:
                    raise ProtocolError('Snapshot subscription unexpectedly carries a resume ID')
                yield frame


def event_payload(frame):
    try:
        value = json.loads(frame.data)
    except ValueError as error:
        raise ProtocolError('Invalid SSE JSON') from error
    if not isinstance(value, dict):
        raise ProtocolError('Expected an SSE object')
    if value.get('type') != frame.event:
        raise ProtocolError('SSE event type mismatch')
    if frame.id and value.get('id') and frame.id != value['id']:
        raise ProtocolError('SSE state ID mismatch')
    return value
