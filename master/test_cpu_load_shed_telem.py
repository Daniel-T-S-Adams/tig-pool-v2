"""Load-shed must respect v1.5 runtime telem (idle ≠ overload)."""

from master.cpu_tier_caps import (
    cpu_earnable_concurrent_ceiling,
    telem_slave_is_working,
    telemetry_requires_load_shed,
)

SETTINGS = {
    "cpu_tier_caps": {"S": 1, "M": 1, "L": 2, "XL": 2},
    "cpu_concurrent_requires_telemetry": True,
    "headroom_ratio": 1.25,
    "load_ok_mult": 0.85,
    "load_shed_mult": 1.25,
    "min_free_ram_gb": 4.0,
    "load_shed_cooldown_ms": 600_000,
    "load_shed_require_active": True,
    "live_telemetry_enabled": True,
}


def test_idle_high_load_does_not_shed():
    telem = {
        "cores": 32,
        "num_workers": 32,
        "load_1m": 55.0,
        "free_ram_gb": 29.0,
        "state": "idle",
        "active_batches": 0,
    }
    assert telem_slave_is_working(telem) is False
    assert telemetry_requires_load_shed(telem, SETTINGS) is False
    assert (
        cpu_earnable_concurrent_ceiling(
            tier=1, telemetry=telem, settings=SETTINGS, load_shed_active=False
        )
        == 1
    )


def test_running_high_load_does_shed():
    telem = {
        "cores": 32,
        "num_workers": 32,
        "load_1m": 55.0,
        "free_ram_gb": 29.0,
        "state": "running",
        "active_batches": 1,
    }
    assert telem_slave_is_working(telem) is True
    assert telemetry_requires_load_shed(telem, SETTINGS) is True
    assert (
        cpu_earnable_concurrent_ceiling(
            tier=1, telemetry=telem, settings=SETTINGS, load_shed_active=False
        )
        == 0
    )


def test_stock_slave_legacy_load_shed():
    # No state/active → keep load-only behavior.
    telem = {"cores": 32, "load_1m": 55.0, "free_ram_gb": 60.0}
    assert telem_slave_is_working(telem) is None
    assert telemetry_requires_load_shed(telem, SETTINGS) is True


def test_idle_low_ram_still_sheds():
    telem = {
        "cores": 32,
        "load_1m": 10.0,
        "free_ram_gb": 1.5,
        "state": "idle",
        "active_batches": 0,
    }
    assert telemetry_requires_load_shed(telem, SETTINGS) is True


def test_require_active_can_disable():
    telem = {
        "cores": 32,
        "load_1m": 55.0,
        "free_ram_gb": 29.0,
        "state": "idle",
        "active_batches": 0,
    }
    settings = dict(SETTINGS, load_shed_require_active=False)
    assert telemetry_requires_load_shed(telem, settings) is True


if __name__ == "__main__":
    test_idle_high_load_does_not_shed()
    test_running_high_load_does_shed()
    test_stock_slave_legacy_load_shed()
    test_idle_low_ram_still_sheds()
    test_require_active_can_disable()
    print("ok")
