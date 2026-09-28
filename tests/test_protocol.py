import json

import httpx
import pytest

from anytype_plugin.client import Client, Frame, ProtocolError, event_payload, parse_sse
from anytype_plugin.config import Settings, Target
from anytype_plugin.formatting import escape, object_link, plain, references, render_chunks, utf16_len


class SplitStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        data = ('\ufeff: keepalive\r\n\r\nid: s1\r\nevent: message_added\r\n'
                'data: {"id":"s1",\r\ndata: "type":"message_added","message":{"text":"🙂"}}\r\n\r\n'
                'id: s2\r\n\r\n').encode()
        for byte in data:
            yield bytes([byte])


async def test_fragmented_sse_retains_id_only_and_unicode():
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={'Content-Type': 'text/event-stream'}, stream=SplitStream()))
    client = Client('http://localhost:31009', 'test', transport=transport)
    frames = [frame async for frame in client.stream(Target('space.full', 'chat'), 'old')]
    await client.close()
    assert event_payload(frames[0])['message']['text'] == '🙂'
    assert frames[1].id == 's2' and frames[1].data == ''


async def test_unterminated_frame_not_committed():
    async def lines():
        for line in ['id: unfinished', 'data: {}']:
            yield line
    assert [f async for f in parse_sse(lines())] == []


def test_bad_event_identity_rejected():
    with pytest.raises(ProtocolError):
        event_payload(Frame('message_added', '{"id":"different","type":"message_added"}', 's1'))


async def test_v2_pagination_uses_top_level_has_more():
    seen = []
    def handler(request):
        seen.append(request.url.params['offset'])
        assert request.headers['Authorization'] == 'Bearer test'
        return httpx.Response(200, json={'data': [{'id': str(len(seen))}], 'has_more': len(seen) < 2})
    client = Client('http://localhost:31009', 'test', transport=httpx.MockTransport(handler))
    rows = [x async for x in client.rows('/v2/spaces/space/members')]
    await client.close()
    assert seen == ['0', '1'] and [x['id'] for x in rows] == ['1', '2']


def test_object_links_survive_long_unicode_output():
    link = object_link('space.full', 'report', 'Research [A] 🙂')
    original = '🙂' * 4100 + '\n\n' + link + ' end'
    chunks = render_chunks(original)
    assert len(chunks) > 1 and all(utf16_len(x) <= 8000 for x in chunks)
    assert ''.join(chunks) == original
    assert any(link in chunk for chunk in chunks)
    assert references(' '.join(chunks), 'space.full') == [('space.full', 'report')]


def test_markdown_passes_through_unescaped():
    text = '**First**\n- **[Report](https://mail.google.com/x)** — asking\n`code` and _em_'
    assert render_chunks(text) == [text]


def test_plain_fallback_escapes_markup_without_breaking_links():
    assert plain('*literal* [brackets]') == escape('*literal* [brackets]')
    link = '[Example](https://example.com/page)'
    assert plain(link) == link


def test_chunks_split_at_paragraph_break():
    first, second = 'a' * 5000 + '\n\n', 'b' * 5000
    assert render_chunks(first + second) == [first, second]


def test_chunks_split_at_line_break_without_paragraph_break():
    first, second = 'a' * 5000 + '\n', 'b' * 5000
    assert render_chunks(first + second) == [first, second]


def test_every_chunk_fits_after_plain_fallback():
    text = '*' * 9000
    chunks = render_chunks(text)
    assert ''.join(chunks) == text
    assert all(utf16_len(plain(chunk)) <= 8000 for chunk in chunks)


def test_emphasis_wrapped_object_link_is_referenced():
    text = '**' + object_link('space.full', 'report', 'Report') + '**'
    assert references(text, 'space.full') == [('space.full', 'report')]


def test_cross_space_reference_and_validation():
    assert references(object_link('other.full', 'obj'), 'space.full') == [('other.full', 'obj')]
    with pytest.raises(ValueError):
        Target.parse('chat-only')
    with pytest.raises(ValueError):
        Settings.load({'chats': [], 'group_allow_from': ['*']})
