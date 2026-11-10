"""Configuration contains routing/policy only; credentials stay profile scoped."""

from dataclasses import dataclass
import re
from urllib.parse import urlsplit


def api_key_name(extra):
    """An explicit selector is authoritative, including when its value is missing."""
    name = extra.get('api_key_env', 'ANYTYPE_API_KEY')
    if not isinstance(name, str) or not re.fullmatch(r'ANYTYPE_[A-Z0-9_]+', name):
        raise ValueError('api_key_env must be an ANYTYPE_ environment variable name')
    return name


def resolve_api_key(extra, *, required=True):
    from gateway.platforms._shared import get_scoped_secret
    name = api_key_name(extra)
    value = get_scoped_secret(name, '', external_fallback=True)
    if required and not value:
        raise ValueError(f'The selected Anytype credential ({name}) is missing')
    return value


@dataclass(frozen=True)
class Target:
    space_id: str
    chat_id: str

    def __post_init__(self):
        for value in (self.space_id, self.chat_id):
            if not isinstance(value, str) or not value or any(c in value for c in '/?#<> \r\n'):
                raise ValueError('Use full, nonempty space/chat IDs without URL delimiters')

    @property
    def key(self):
        return f'{self.space_id}/{self.chat_id}'

    @classmethod
    def parse(cls, value):
        parts = str(value).split('/')
        if len(parts) != 2:
            raise ValueError('Anytype target must be <full-space-id>/<chat-id>')
        return cls(*parts)


def identities(value, name):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f'{name} must be a list of nonempty identity strings')
    result = frozenset(x.strip() for x in value)
    if '*' in result and result != {'*'}:
        raise ValueError(f'{name}: wildcard must be the only entry')
    return result


@dataclass(frozen=True)
class Settings:
    base_url: str
    targets: tuple[Target, ...]
    allow: frozenset[str]
    admins: frozenset[str]
    trigger: str
    prefix: str
    attach_object_links: bool
    activity_status: bool = True
    discovery_space: str | None = None
    watch_agent_objects: bool = True
    discovered_trigger: str = 'all'
    discovery_all_spaces: bool = False
    api_key_env: str = 'ANYTYPE_API_KEY'

    @property
    def discovery_enabled(self):
        return bool(self.discovery_space or self.discovery_all_spaces)

    def discovers(self, space):
        return self.discovery_all_spaces or self.discovery_space == space

    @property
    def spaces(self):
        return tuple(dict.fromkeys(([self.discovery_space] if self.discovery_space else []) +
                                   [t.space_id for t in self.targets]))

    @classmethod
    def load(cls, extra):
        key_name = api_key_name(extra)
        url = str(extra.get('base_url', 'http://127.0.0.1:31009')).rstrip('/')
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('base_url must be an HTTP(S) origin, without credentials or path')
        targets = tuple(Target(**row) for row in extra.get('chats', []))
        discovery = extra.get('discovery') or {}
        if not isinstance(discovery, dict):
            raise ValueError('discovery must be an object with space_id')
        space = discovery.get('space_id')
        all_spaces = discovery.get('spaces') == 'all'
        if 'spaces' in discovery and not all_spaces:
            raise ValueError('discovery.spaces must be all')
        if space and all_spaces:
            raise ValueError('Choose discovery.space_id or discovery.spaces: all')
        if discovery and not (space or all_spaces):
            raise ValueError('discovery.space_id or discovery.spaces: all is required')
        if space:
            Target(space, 'validate')
            if any(t.space_id != space for t in targets):
                raise ValueError('Multi-chat discovery supports one space per profile; all pinned chats must belong to it')
        if (not targets and not (space or all_spaces)) or len(targets) > 64 or len(set(targets)) != len(targets):
            raise ValueError('Configure between 1 and 64 distinct Anytype chats')
        owned = discovery.get('watch_agent_objects', True)
        discovered_trigger = discovery.get('response', 'all')
        if not isinstance(owned, bool) or discovered_trigger not in {'all', 'mentions'}:
            raise ValueError('discovery requires boolean watch_agent_objects and response: all | mentions')
        trigger = extra.get('trigger', 'all')
        prefix = extra.get('prefix', '!hermes')
        if trigger not in {'all', 'prefix'} or not isinstance(prefix, str) or not prefix.strip():
            raise ValueError('trigger must be all or prefix, with a nonempty prefix')
        if extra.get('startup_history', 'skip') != 'skip':
            raise ValueError('Only startup_history: skip is supported')
        if extra.get('send_read_receipts', False) is not False:
            raise ValueError('Read receipts are not supported in version 0.1')
        attach = extra.get('attach_object_links', True)
        if not isinstance(attach, bool):
            raise ValueError('attach_object_links must be boolean')
        allow = identities(extra.get('group_allow_from', []), 'group_allow_from')
        admins = identities(extra.get('group_allow_admin_from', []), 'group_allow_admin_from')
        if '*' in admins:
            raise ValueError('Admin identities must be explicit')
        activity = extra.get('activity_status', True)
        if not isinstance(activity, bool):
            raise ValueError('activity_status must be boolean')
        return cls(url, targets, allow, admins, trigger, prefix, attach, activity,
                   space, owned, discovered_trigger, all_spaces, key_name)
