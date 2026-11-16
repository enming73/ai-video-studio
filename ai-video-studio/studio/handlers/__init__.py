"""Handler registration for the persistent worker."""
from __future__ import annotations


def register_all() -> None:
    from ..worker import register
    from . import image, llm_narration, llm_prompt, llm_storyboard, tts_segment, video, vision_review
    register('video', video)
    register('llm_storyboard', llm_storyboard)
    register('image', image)
    register('llm_prompt', llm_prompt)
    register('llm_narration', llm_narration)
    register('tts_segment', tts_segment)
    register('vision_review', vision_review)
