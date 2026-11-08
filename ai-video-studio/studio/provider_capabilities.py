"""Verified text-to-video duration capabilities, in whole seconds."""

_CAPABILITIES = {
    ('mock', 'mock'): (range(1, 31), 'Local mock generator'),
    ('minimax', 'MiniMax-H3'): (range(4, 16), 'https://platform.minimax.io/docs/api-reference/video-generation-v2-create'),
    ('wan', 'wan3.0-video'): (range(2, 31), 'https://www.alibabacloud.com/help/en/model-studio/wan3-video-generation-api-reference'),
    ('flux', 'blackforestlabs/flux-3-max/text-to-video'): (range(5, 21), 'https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=blackforestlabs%2Fflux-3-max%2Ftext-to-video'),
}

_I2V = {
    ('mock', 'mock'): (range(1, 31), 'Local mock; no AI video generation'),
    ('minimax', 'MiniMax-H3'): (range(4, 16), 'https://platform.minimax.io/docs/api-reference/video-generation-v2-create'),
}


def i2v_capabilities(provider: str, model: str) -> dict:
    item = _I2V.get((provider, model))
    if item is None: return {'verified': False, 'durations': [], 'source': None}
    return {'verified': True, 'durations': list(item[0]), 'source': item[1]}


def capabilities(provider: str, model: str) -> dict:
    item = _CAPABILITIES.get((provider, model))
    if item is None:
        return {'verified': False, 'durations': [], 'source': None}
    durations, source = item
    return {'verified': True, 'durations': list(durations), 'source': source}


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
