"""Hermes discovers this directory as an external platform plugin."""

from pathlib import Path


def register(ctx):
    from .adapter import register as register_platform

    register_platform(ctx)
    ctx.register_skill('anytype', Path(__file__).parent / 'skills' / 'anytype' / 'SKILL.md',
                       description='Work with objects, research and tasks in Anytype.')
