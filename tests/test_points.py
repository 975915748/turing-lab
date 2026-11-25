import pytest

from points import calculate_account_result, calculate_streak_reward


def test_streak_rewards_match_required_half_up_schedule():
    assert [calculate_streak_reward(n) for n in range(1, 7)] == [10, 12, 14, 17, 21, 25]


def test_loss_resets_streak_and_next_win_starts_at_ten():
    assert calculate_account_result(36, 3, False) == (26, 0, -10)
    assert calculate_account_result(26, 0, True) == (36, 1, 10)


def test_loss_floor_is_applied_to_actual_delta():
    assert calculate_account_result(-95, 0, False) == (-100, 0, -5)
    assert calculate_account_result(-100, 0, False) == (-100, 0, 0)


def test_large_streak_uses_integer_arithmetic():
    reward = calculate_streak_reward(100)
    assert isinstance(reward, int)
    assert reward > 0
    assert calculate_account_result(0, 99, True)[1] == 100


@pytest.mark.parametrize("streak", [0, -1, 1.5, True])
def test_invalid_streak_is_rejected(streak):
    with pytest.raises(ValueError):
        calculate_streak_reward(streak)
