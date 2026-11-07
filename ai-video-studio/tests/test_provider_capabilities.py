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
