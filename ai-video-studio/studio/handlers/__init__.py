"""Handler registration for the persistent worker."""
from __future__ import annotations


def register_all() -> None:
    from ..worker import register
    from . import video
    register('video', video)
