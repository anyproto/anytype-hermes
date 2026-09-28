"""Readable inline text, navigable links, and UTF-16-safe message chunks."""

import re
from urllib.parse import parse_qs, quote, urlencode, urlsplit


LIMIT = 8000
# A label may contain escaped brackets; unescaped nested markup is plain text.
LINK = re.compile(r'\[((?:\\.|[^\[\]\\])*)\]\(([^\s]+?)\)(?=\s|[.,!?;:]|$)')
SPECIAL = re.compile(r'([\\*\[\]_<`~])')


def utf16_len(value):
    return len(value.encode('utf-16-le')) // 2


def escape(value):
    return SPECIAL.sub(r'\\\1', value)


def object_link(space_id, object_id, title=None):
    url = 'anytype://object?' + urlencode({'objectId': object_id, 'spaceId': space_id}, quote_via=quote)
    return f'[{escape(title or object_id)}]({url})'


def object_reference(url, default_space):
    parsed = urlsplit(url)
    if parsed.scheme != 'anytype' or parsed.netloc != 'object' or parsed.path not in {'', '/'}:
        return None
    params = parse_qs(parsed.query)
    ids, spaces = params.get('objectId', []), params.get('spaceId', [default_space])
    if len(ids) != 1 or len(spaces) != 1 or not ids[0] or not spaces[0]:
        return None
    if any(c in ids[0] + spaces[0] for c in '/\r\n\x00'):
        return None
    return spaces[0], ids[0]


def _plain_tokens(text):
    # Escape before chunking and keep each escape pair/code point intact.
    for character in text:
        yield escape(character)


def tokens(text):
    position = 0
    for match in LINK.finditer(text):
        yield from _plain_tokens(text[position:match.start()])
        label, url = match.groups()
        parsed = urlsplit(url)
        if parsed.scheme in {'http', 'https', 'anytype'}:
            label = re.sub(r'\\(.)', r'\1', label)
            label = escape(label)
            link = f'[{label}]({url})'
            if utf16_len(link) > LIMIT:
                budget = LIMIT - utf16_len(f'[]({url})') - 1
                if budget < 1:
                    raise ValueError('A link URL exceeds the Anytype message limit')
                pieces, used = [], 0
                for token in _plain_tokens(re.sub(r'\\(.)', r'\1', label)):
                    if used + utf16_len(token) > budget:
                        break
                    pieces.append(token)
                    used += utf16_len(token)
                link = f'[{"".join(pieces)}…]({url})'
            yield link
        else:
            yield from _plain_tokens(match.group())
        position = match.end()
    yield from _plain_tokens(text[position:])


def render_chunks(text, limit=LIMIT):
    result, current, units = [], [], 0
    for token in tokens(text):
        size = utf16_len(token)
        if size > limit:
            raise ValueError('An indivisible link exceeds the chunk limit')
        if units + size > limit:
            # Prefer a recent whitespace boundary, retaining all input characters.
            cut = next((i + 1 for i in range(len(current) - 1, max(-1, len(current) - 200), -1)
                        if current[i].isspace()), len(current))
            result.append(''.join(current[:cut]))
            current = current[cut:]
            units = sum(utf16_len(x) for x in current)
            if units + size > limit:
                result.append(''.join(current))
                current, units = [], 0
        current.append(token)
        units += size
    if current:
        result.append(''.join(current))
    return [chunk for chunk in result if chunk]


def references(text, default_space):
    return list(dict.fromkeys(ref for match in LINK.finditer(text)
                              if (ref := object_reference(match[2], default_space))))
