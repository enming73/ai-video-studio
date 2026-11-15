"""Verified provider capabilities, delegated to the shared provider contract."""
from __future__ import annotations

from video_provider_core import Flux, MiniMax, Wan

# Capabilities are static metadata from the real adapters, independent of any
# runtime adapter substitution used by tests or future routing.
_REAL = {'minimax': MiniMax, 'flux': Flux, 'wan': Wan}

_MOCK = {
    'text': (range(1, 31), 'Local mock generator'),
    'i2v': (range(1, 31), 'Local mock; no AI video generation'),
}


def _contract(provider: str, model: str, i2v: bool):
    if provider == 'mock':
        durations, source = _MOCK['i2v' if i2v else 'text']
        return {'durations': list(durations), 'source': source,
                'first_last_frame': i2v}
    cls = _REAL.get(provider)
    if cls is None:
        return None
    try:
        capability = cls(None).capabilities(model=model, i2v=i2v)
    except Exception:
        # Invalid local provider configuration must not crash capability checks.
        return None
    if capability is None:
        return None
    return {'durations': list(capability.durations),
            'source': capability.source,
            'first_last_frame': capability.supports_first_frame
            and capability.supports_last_frame}


def i2v_capabilities(provider: str, model: str) -> dict:
    item = _contract(provider, model, True)
    if item is None:
        return {'verified': False, 'durations': [], 'source': None,
                'first_last_frame': False}
    return {'verified': True, **item}


def capabilities(provider: str, model: str) -> dict:
    item = _contract(provider, model, False)
    if item is None:
        return {'verified': False, 'durations': [], 'source': None}
    return {'verified': True, 'durations': item['durations'],
            'source': item['source']}


def _check(total, allowed):
    if type(total) is not int or not 1 <= total <= 600:
        raise ValueError('Total duration must be an integer between 1 and 600')
    if any(type(x) is not int or x <= 0 for x in allowed):
        raise ValueError('Allowed durations must be positive integers')


def feasible(total: int, allowed: list[int]) -> bool:
    _check(total, allowed)
    reachable = [True] + [False] * total
    for seconds in range(1, total + 1):
        reachable[seconds] = any(d <= seconds and reachable[seconds - d] for d in allowed)
    return reachable[total]


def validate_durations(total: int, durations: list[int], allowed: list[int]) -> None:
    _check(total, allowed)
    if not durations or any(type(d) is not int or d not in allowed for d in durations):
        raise ValueError('One or more shot durations are unsupported')
    if sum(durations) != total:
        raise ValueError('Shot durations must sum to the project duration')
