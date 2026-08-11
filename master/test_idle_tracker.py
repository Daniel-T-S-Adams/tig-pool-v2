"""Lightweight checks for IdleWindowTracker (no DB)."""

from master.idle_tracker import IdleWindowTracker


def test_between_job_flicker_not_sustained():
    t = IdleWindowTracker()
    # Mostly busy; only short empty gaps between jobs.
    t.update(0, ["cpu-a"], [], window_ms=60_000)
    t.update(18_000, ["cpu-a"], [], window_ms=60_000)  # busy
    t.update(20_000, ["cpu-a"], ["cpu-a"], window_ms=60_000)  # 2s idle gap starts
    t.update(22_000, ["cpu-a"], [], window_ms=60_000)  # busy again
    t.update(40_000, ["cpu-a"], [], window_ms=60_000)
    t.update(41_000, ["cpu-a"], ["cpu-a"], window_ms=60_000)  # 1s idle
    t.update(42_000, ["cpu-a"], [], window_ms=60_000)
    t.update(60_000, ["cpu-a"], [], window_ms=60_000)
    settings = {
        "enabled": True,
        "window_ms": 60_000,
        "frac_threshold": 0.5,
        "min_observed_ms": 30_000,
        "min_continuous_ms": 15_000,
    }
    assert t.idle_frac("cpu-a") < 0.2, t.idle_frac("cpu-a")
    assert not t.is_sustained_idle("cpu-a", settings)


def test_chronic_idle_is_sustained():
    t = IdleWindowTracker()
    t.update(0, ["cpu-a"], ["cpu-a"], window_ms=60_000)
    t.update(40_000, ["cpu-a"], ["cpu-a"], window_ms=60_000)
    settings = {
        "enabled": True,
        "window_ms": 60_000,
        "frac_threshold": 0.5,
        "min_observed_ms": 30_000,
        "min_continuous_ms": 15_000,
    }
    assert t.idle_frac("cpu-a") >= 0.99
    assert t.is_sustained_idle("cpu-a", settings)


def test_warmup_requires_continuous():
    t = IdleWindowTracker()
    t.update(0, ["cpu-a"], ["cpu-a"], window_ms=60_000)
    t.update(5_000, ["cpu-a"], ["cpu-a"], window_ms=60_000)
    settings = {
        "enabled": True,
        "window_ms": 60_000,
        "frac_threshold": 0.5,
        "min_observed_ms": 30_000,
        "min_continuous_ms": 15_000,
    }
    # Only 5s continuous so far → not sustained during warm-up.
    assert not t.is_sustained_idle("cpu-a", settings)
    t.update(20_000, ["cpu-a"], ["cpu-a"], window_ms=60_000)
    assert t.is_sustained_idle("cpu-a", settings)


if __name__ == "__main__":
    test_between_job_flicker_not_sustained()
    test_chronic_idle_is_sustained()
    test_warmup_requires_continuous()
    print("ok")
