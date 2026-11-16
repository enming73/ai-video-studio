"""Handler registration for the persistent worker."""
from __future__ import annotations


def register_all() -> None:
    from ..worker import register
    from . import image, llm_prompt, llm_storyboard, video
    register('video', video)
    register('llm_storyboard', llm_storyboard)
    register('image', image)
    register('llm_prompt', llm_prompt)
