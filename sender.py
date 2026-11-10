"""One persisted operation per logical reply, one API receipt per chunk."""

import asyncio
import hashlib
import json
import uuid
from urllib.parse import quote

import httpx

from gateway.platforms.base import SendResult

from .client import APIError, ProtocolError
from .formatting import plain, references, render_chunks


class Sender:
    def __init__(self, client, store, settings):
        self.client, self.store, self.settings = client, store, settings
        self._lock = asyncio.Lock()

    async def _payloads(self, target, content, reply_to, metadata):
        chunks = render_chunks(content) or ['']
        explicit = metadata.get('attachments', [])
        if not isinstance(explicit, list) or any(not isinstance(x, str) or not x for x in explicit):
            raise ValueError('attachments must be a list of Anytype object IDs')
        seen, payloads = set(), []
        for i, chunk in enumerate(chunks):
            ids = list(explicit) if i == 0 else []
            if self.settings.attach_object_links:
                ids.extend(obj for space, obj in references(chunk, target.space_id) if space == target.space_id)
            valid = []
            for obj in ids:
                if obj in seen:
                    continue
                seen.add(obj)
                try:
                    await self.client.request('GET', f'/v2/spaces/{quote(target.space_id, safe="")}/objects/{quote(obj, safe="")}')
                except APIError as error:
                    if error.status in {400, 403, 404}:
                        continue  # Inline links remain useful even when a preview is unavailable.
                    raise
                valid.append(obj)
            groups = [valid[n:n + 32] for n in range(0, len(valid), 32)] or [[]]
            for j, group in enumerate(groups):
                payload = {'text': chunk if j == 0 else 'Additional attachments.'}
                if group:
                    payload['attachments'] = group
                if reply_to:
                    payload['reply_to'] = reply_to
                if payload['text'] or group:
                    payloads.append(payload)
        if not payloads:
            raise ValueError('Cannot send an empty message')
        return payloads

    async def _deliver(self, target, operation, row):
        payload = json.loads(row['payload'])
        try:
            return await self.client.send(target, payload, row['request_key'])
        except APIError as error:
            fallback = plain(payload['text'])
            if error.status != 400 or fallback == payload['text']:
                raise
        # Anytype rejected the markup; resend it as literal text. Heart stores only
        # successful idempotent results, so the key is still free. Persist first so
        # a crash replays the fallback rather than conflicting with it.
        payload['text'] = fallback
        self.store.delivery_payload(operation, row['chunk'], payload)
        return await self.client.send(target, payload, row['request_key'])

    async def send(self, target, content, reply_to=None, metadata=None):
        metadata = metadata or {}
        operation = metadata.get('delivery_id')
        if not operation:
            # Stock Hermes gives ordinary replies an incoming anchor. It does not
            # pass a stable operation ID to recovered or standalone sends.
            fingerprint = json.dumps([target.key, reply_to, content, metadata.get('attachments', [])])
            operation = ('reply:' + hashlib.sha256(fingerprint.encode()).hexdigest()
                         if reply_to else 'standalone:' + str(uuid.uuid4()))
        async with self._lock:
            try:
                rows = self.store.delivery(operation)
                if rows:
                    # Validate operation binding without rebuilding attachments from changing objects.
                    rows = self.store.prepare_delivery(operation, target.key, content, [])
                else:
                    payloads = await self._payloads(target, content, reply_to, metadata)
                    rows = self.store.prepare_delivery(operation, target.key, content, payloads)
                remote_ids = []

                def failure(error, **kwargs):
                    # Without an anchor/explicit ID a subsequent send has a fresh
                    # identity. Let Hermes defer partial delivery to its marked
                    # recovery instead of repeating already-visible chunks inline.
                    partial = bool(remote_ids) and not (reply_to or metadata.get('delivery_id'))
                    return SendResult(False, error=error,
                                      raw_response={'partial_overflow': True} if partial else None,
                                      **kwargs)

                for row in rows:
                    if row['status'] == 'delivered':
                        remote_ids.append(row['remote_id'])
                        continue
                    if row['status'] in {'sending', 'uncertain', 'failed'}:
                        return failure(f'anytype_delivery_{row["status"]}:{operation}')
                    self.store.delivery_status(operation, row['chunk'], 'sending')
                    try:
                        remote_id = await self._deliver(target, operation, row)
                    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                        self.store.delivery_status(operation, row['chunk'], 'pending')
                        return failure('Anytype ConnectError before delivery', retryable=True)
                    except APIError as error:
                        if error.status == 429:
                            self.store.delivery_status(operation, row['chunk'], 'pending')
                            return failure(f'flood_control:{error.retry_after or 60}',
                                           retry_after=error.retry_after or 60)
                        status = 'uncertain' if error.status >= 500 else 'failed'
                        self.store.delivery_status(operation, row['chunk'], status)
                        return failure(f'anytype_delivery_{status}:{operation}:HTTP{error.status}')
                    except (httpx.HTTPError, ProtocolError):
                        self.store.delivery_status(operation, row['chunk'], 'uncertain')
                        return failure(f'anytype_delivery_uncertain:{operation}')
                    self.store.delivery_status(operation, row['chunk'], 'delivered', remote_id)
                    remote_ids.append(remote_id)
                return SendResult(True, message_id=remote_ids[-1], continuation_message_ids=tuple(remote_ids[:-1]))
            except (ValueError, APIError, httpx.HTTPError, ProtocolError) as error:
                return SendResult(False, error=f'Anytype send preparation failed: {type(error).__name__}')
