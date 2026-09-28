"""Hermes discovers this directory as an external platform plugin."""


def register(ctx):
    from .adapter import register as register_platform

    register_platform(ctx)
