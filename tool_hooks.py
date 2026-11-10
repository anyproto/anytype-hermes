"""Observe Hermes's public tool hooks inside the originating adapter task.

Hermes copies ContextVars into agent and hook workers. The binding travels with
that work; it never looks up a mutable 'current chat' or routes from environment
variables. All activity bookkeeping and publication happen on the gateway loop.
"""

import logging
from contextvars import ContextVar

log = logging.getLogger(__name__)
_binding = ContextVar('anytype_activity_binding', default=None)


class Binding:
    def __init__(self, publisher, target):
        self.publisher, self.target = publisher, target
        self.runs = {}
        self.finished = set()
        self.closed = False

    def enqueue(self, event, fields):
        self.publisher.loop.call_soon_threadsafe(self._handle, event, fields)

    def _handle(self, event, fields):
        if self.closed:
            return
        try:
            turn = (fields['session_id'], fields['turn_id'])
            if event == 'post_llm_call':
                self.finished.add(turn)
                run = self.runs.pop(turn, None)
                if run:
                    run.close()
                return
            if turn in self.finished:
                return
            run = self.runs.setdefault(turn, None)
            if run is None:
                run = self.runs[turn] = self.publisher.run(self.target)
            # Provider call IDs can be reused on a subsequent model request.
            call = (fields.get('api_request_id', ''), fields['tool_call_id'])
            name, args = fields.get('tool_name', ''), fields.get('args', {})
            if event == 'pre_tool_call':
                run.start(call, name, args, label=f'Preparing {name}')
            else:
                run.complete(call, name, args, fields.get('result'), status=fields.get('status'))
        except Exception:
            # An observer must never change tool policy, execution, or its result.
            log.debug('Anytype tool activity update failed', exc_info=True)

    def close(self):
        self.publisher.loop.call_soon_threadsafe(self._close)

    def _close(self):
        self.closed = True
        for run in self.runs.values():
            run.close()
        self.runs.clear()
        self.finished.clear()


def bind(publisher, target):
    binding = Binding(publisher, target)
    _binding.set(binding)
    return binding


def unbind(binding):
    if _binding.get() is binding:
        _binding.set(None)
    binding.close()


def observe(event, fields):
    try:
        binding = _binding.get()
        if binding is None:
            return
        from agent.delegation_context import is_delegated_child_context
        from agent.notification_presentation import event_presentation_muted
        from gateway.session_context import get_session_env
        session_id, turn_id = fields.get('session_id'), fields.get('turn_id')
        if (not session_id or not turn_id or is_delegated_child_context()
                or event_presentation_muted('tool.start', session_id)
                or get_session_env('HERMES_SESSION_PLATFORM') != 'anytype'
                or get_session_env('HERMES_SESSION_CHAT_ID') != binding.target.key
                or get_session_env('HERMES_CRON_SESSION') == '1'):
            return
        if event != 'post_llm_call' and not fields.get('tool_call_id'):
            return  # no trustworthy execution identity; native typing still works
        binding.enqueue(event, fields)
    except Exception:
        log.debug('Anytype tool hook ignored', exc_info=True)
    # In particular, pre_tool_call must return None (no policy/argument change).


def pre_tool_call(**fields):
    observe('pre_tool_call', fields)


def post_tool_call(**fields):
    observe('post_tool_call', fields)


def post_llm_call(**fields):
    observe('post_llm_call', fields)


def register(ctx):
    for name, callback in [('pre_tool_call', pre_tool_call), ('post_tool_call', post_tool_call),
                           ('post_llm_call', post_llm_call)]:
        ctx.register_hook(name, callback)
