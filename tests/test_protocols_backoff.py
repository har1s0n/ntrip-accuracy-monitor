from __future__ import annotations

import random

import pytest

from ntrip_accuracy_monitor.protocols.backoff import BackoffPolicy


class TestBackoffPolicyValidation:
    def test_rejects_zero_initial_delay(self) -> None:
        with pytest.raises(ValueError, match="initial_delay_s"):
            BackoffPolicy(initial_delay_s=0.0, max_delay_s=10.0)

    def test_rejects_negative_initial_delay(self) -> None:
        with pytest.raises(ValueError, match="initial_delay_s"):
            BackoffPolicy(initial_delay_s=-1.0, max_delay_s=10.0)

    def test_rejects_max_below_initial(self) -> None:
        with pytest.raises(ValueError, match="max_delay_s"):
            BackoffPolicy(initial_delay_s=5.0, max_delay_s=1.0)

    def test_rejects_multiplier_below_one(self) -> None:
        with pytest.raises(ValueError, match="multiplier"):
            BackoffPolicy(initial_delay_s=1.0, max_delay_s=10.0, multiplier=0.5)

    def test_rejects_jitter_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="jitter"):
            BackoffPolicy(initial_delay_s=1.0, max_delay_s=10.0, jitter=1.5)
        with pytest.raises(ValueError, match="jitter"):
            BackoffPolicy(initial_delay_s=1.0, max_delay_s=10.0, jitter=-0.1)


class TestBackoffPolicyDelay:
    def test_exponential_growth_without_jitter(self) -> None:
        policy = BackoffPolicy(
            initial_delay_s=1.0, max_delay_s=100.0, multiplier=2.0, jitter=0.0,
        )
        assert policy.delay_for_attempt(0) == pytest.approx(1.0)
        assert policy.delay_for_attempt(1) == pytest.approx(2.0)
        assert policy.delay_for_attempt(2) == pytest.approx(4.0)
        assert policy.delay_for_attempt(5) == pytest.approx(32.0)

    def test_caps_at_max_delay(self) -> None:
        policy = BackoffPolicy(
            initial_delay_s=1.0, max_delay_s=10.0, multiplier=2.0, jitter=0.0,
        )
        assert policy.delay_for_attempt(20) == pytest.approx(10.0)

    def test_jitter_within_band(self) -> None:
        policy = BackoffPolicy(
            initial_delay_s=1.0, max_delay_s=100.0, multiplier=2.0, jitter=0.2,
        )
        rng = random.Random(42)
        for attempt in range(8):
            base = min(2.0 ** attempt, 100.0)
            d = policy.delay_for_attempt(attempt, rng=rng)
            assert base * 0.8 <= d <= base * 1.2

    def test_jitter_never_returns_negative(self) -> None:
        policy = BackoffPolicy(
            initial_delay_s=0.001, max_delay_s=1.0, multiplier=2.0, jitter=1.0,
        )
        rng = random.Random(0)
        for _ in range(200):
            assert policy.delay_for_attempt(0, rng=rng) >= 0.0

    def test_negative_attempt_rejected(self) -> None:
        policy = BackoffPolicy(initial_delay_s=1.0, max_delay_s=10.0)
        with pytest.raises(ValueError, match="attempt"):
            policy.delay_for_attempt(-1)
