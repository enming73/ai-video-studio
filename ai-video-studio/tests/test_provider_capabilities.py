import pytest

from studio import provider_capabilities as pc


def test_duration_validation_uses_total_and_allowed_values():
    pc.validate_durations(20, [8, 12], [8, 12])
    with pytest.raises(ValueError):
        pc.validate_durations(20, [8, 8], [8, 12])
    with pytest.raises(ValueError):
        pc.validate_durations(20, [8, 13], [8, 12])


def test_feasibility_and_unverified_provider():
    assert pc.feasible(20, [8, 12])
    assert not pc.feasible(21, [8, 12])
    assert not pc.feasible(20, [])
    assert not pc.capabilities('unknown', 'model')['verified']


@pytest.mark.parametrize('total,allowed', [(True, [1]), (0, [1]), (20, [False]), (20, [1.5])])
def test_invalid_feasibility_inputs_are_rejected(total, allowed):
    with pytest.raises(ValueError):
        pc.feasible(total, allowed)


def test_flux_schema_lists_each_second_from_five_to_twenty():
    result = pc.capabilities('flux', 'blackforestlabs/flux-3-max/text-to-video')
    assert result['verified']
    assert result['durations'] == list(range(5, 21))


def test_studio_capabilities_match_provider_contract(monkeypatch):
    from video_provider_core.providers import Flux, MiniMax, Wan
    monkeypatch.delenv('WAN_BASE_URL', raising=False)
    cases = [
        ('minimax', 'MiniMax-H3', MiniMax(None), False),
        ('flux', 'blackforestlabs/flux-3-max/text-to-video', Flux(None), False),
        ('wan', 'wan3.0-video', Wan(None), False),
        ('minimax', 'MiniMax-H3', MiniMax(None), True),
        ('flux', 'blackforestlabs/flux-3-max/image-to-video', Flux(None), True),
    ]
    for provider, model, instance, i2v in cases:
        expected = instance.capabilities(model=model, i2v=i2v)
        actual = (pc.i2v_capabilities(provider, model) if i2v
                  else pc.capabilities(provider, model))
        assert actual['verified'] is True
        assert actual['durations'] == list(expected.durations)
        assert actual['source'] == expected.source
        if i2v:
            assert actual['first_last_frame'] is True


def test_invalid_wan_configuration_is_unverified_not_crash(monkeypatch):
    monkeypatch.setenv('WAN_BASE_URL', 'https://evil.example')
    assert pc.capabilities('wan', 'wan3.0-video')['verified'] is False
    assert pc.i2v_capabilities('wan', 'wan3.0-video')['verified'] is False
