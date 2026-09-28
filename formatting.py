"""Markdown message chunks, their escaped plain-text fallback, and navigable links."""

import re
from urllib.parse import parse_qs, quote, urlencode, urlsplit


LIMIT = 8000
# A label may contain escaped brackets; unescaped nested markup is plain text.
# A link may close inside emphasis, e.g. **[label](url)**.
LINK = re.compile(r'\[((?:\\.|[^\[\]\\])*)\]\(([^\s]+?)\)(?=\s|[.,!?;:*_~]|$)')
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
    # Keep each escape pair/code point intact.
    for character in text:
        yield escape(character)


def _plain_link(label, url):
    label = escape(re.sub(r'\\(.)', r'\1', label))
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
    return link


def _pieces(text):
    """(markdown, plain) pairs: one character each, or a whole link no chunk may split."""
    position = 0
    for match in LINK.finditer(text):
        yield from zip(text[position:match.start()], _plain_tokens(text[position:match.start()]))
        label, url = match.groups()
        if urlsplit(url).scheme in {'http', 'https', 'anytype'}:
            link = _plain_link(label, url)
            written = match.group()
            yield (written if utf16_len(written) <= LIMIT else link), link
        else:
            yield from zip(match.group(), _plain_tokens(match.group()))
        position = match.end()
    yield from zip(text[position:], _plain_tokens(text[position:]))


def _size(piece):
    return max(utf16_len(piece[0]), utf16_len(piece[1]))


def plain(text):
    """The fallback for markdown Anytype rejects: markup reads literally, links stay links."""
    return ''.join(p for _, p in _pieces(text))


def _cut(current):
    # Prefer a paragraph break, then a line break, in the chunk's second half;
    # then a recent whitespace boundary, retaining all input characters.
    chars, half = [m for m, _ in current], len(current) // 2
    for i in range(len(chars) - 1, half, -1):
        if chars[i] == '\n' and chars[i - 1] == '\n':
            return i + 1
    for i in range(len(chars) - 1, half, -1):
        if chars[i] == '\n':
            return i + 1
    return next((i + 1 for i in range(len(chars) - 1, max(-1, len(chars) - 200), -1)
                 if chars[i].isspace()), len(chars))


def render_chunks(text, limit=LIMIT):
    """Split markdown into messages that still fit the limit when resent as plain()."""
    result, current, units = [], [], 0
    for piece in _pieces(text):
        size = _size(piece)
        if size > limit:
            raise ValueError('An indivisible link exceeds the chunk limit')
        if units + size > limit:
            cut = _cut(current)
            result.append(''.join(m for m, _ in current[:cut]))
            current = current[cut:]
            units = sum(_size(x) for x in current)
            if units + size > limit:
                result.append(''.join(m for m, _ in current))
                current, units = [], 0
        current.append(piece)
        units += size
    if current:
        result.append(''.join(m for m, _ in current))
    return [chunk for chunk in result if chunk]


def references(text, default_space):
    return list(dict.fromkeys(ref for match in LINK.finditer(text)
                              if (ref := object_reference(match[2], default_space))))
