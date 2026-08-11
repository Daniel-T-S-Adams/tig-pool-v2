"""Time-weighted slave idle fractions (avoid between-job point-sample flicker).

Ops/create previously treated "online + inflight==0 *right now*" as idle. Short
empty gaps between root batches then looked like a chronically idle fleet.

This tracker credits elapsed wall time to idle vs busy per slave, keeps a soft
sliding window, and exposes a sustained-idle count for create/governor bias.

Keep in sync with master/idle_tracker.py (separate Docker images).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set


def _env_bool(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


def idle_window_settings() -> dict:
    return {
        "enabled": _env_bool("IDLE_WINDOW_ENABLED", "true"),
        "window_ms": max(
            5_000,
            int(os.environ.get("IDLE_WINDOW_MS", str(120_000))),
        ),
        "frac_threshold": min(
            1.0,
            max(0.0, float(os.environ.get("IDLE_WINDOW_FRAC_THRESHOLD", "0.5"))),
        ),
        "min_observed_ms": max(
            0,
            int(os.environ.get("IDLE_WINDOW_MIN_OBSERVED_MS", str(30_000))),
        ),
        "min_continuous_ms": max(
            0,
            int(os.environ.get("IDLE_WINDOW_MIN_CONTINUOUS_MS", str(15_000))),
        ),
    }


@dataclass
class _SlaveIdleState:
    last_ts: int
    last_idle: bool
    idle_ms: float = 0.0
    observed_ms: float = 0.0
    continuous_idle_ms: float = 0.0


class IdleWindowTracker:
    """Process-local time-weighted idle tracker."""

    def __init__(self) -> None:
        self._slaves: Dict[str, _SlaveIdleState] = {}

    def reset(self) -> None:
        self._slaves.clear()

    def update(
        self,
        now_ms: int,
        online_names: Iterable[str],
        idle_names: Iterable[str],
        *,
        window_ms: Optional[int] = None,
    ) -> None:
        settings = idle_window_settings()
        window = int(window_ms if window_ms is not None else settings["window_ms"])
        online: Set[str] = {str(n) for n in online_names if n}
        idle: Set[str] = {str(n) for n in idle_names if n} & online

        for name in list(self._slaves.keys()):
            if name not in online:
                del self._slaves[name]

        for name in online:
            is_idle = name in idle
            st = self._slaves.get(name)
            if st is None:
                self._slaves[name] = _SlaveIdleState(
                    last_ts=int(now_ms),
                    last_idle=is_idle,
                )
                continue
            dt = max(0, int(now_ms) - int(st.last_ts))
            if dt > 0:
                if st.last_idle:
                    st.idle_ms += dt
                    st.continuous_idle_ms += dt
                else:
                    st.continuous_idle_ms = 0.0
                st.observed_ms += dt
                if st.observed_ms > window:
                    scale = window / st.observed_ms
                    st.idle_ms *= scale
                    st.observed_ms = float(window)
            st.last_ts = int(now_ms)
            if is_idle:
                if not st.last_idle:
                    st.continuous_idle_ms = 0.0
            else:
                st.continuous_idle_ms = 0.0
            st.last_idle = is_idle

    def idle_frac(self, name: str) -> Optional[float]:
        st = self._slaves.get(name)
        if st is None:
            return None
        if st.observed_ms <= 0:
            return 1.0 if st.last_idle else 0.0
        return max(0.0, min(1.0, st.idle_ms / st.observed_ms))

    def is_sustained_idle(self, name: str, settings: Optional[dict] = None) -> bool:
        settings = settings or idle_window_settings()
        st = self._slaves.get(name)
        if st is None:
            return False
        if not settings.get("enabled", True):
            return bool(st.last_idle)
        min_obs = int(settings.get("min_observed_ms") or 0)
        min_cont = int(settings.get("min_continuous_ms") or 0)
        thresh = float(settings.get("frac_threshold") or 0.5)
        if st.observed_ms < min_obs:
            return bool(st.last_idle) and st.continuous_idle_ms >= min_cont
        return self.idle_frac(name) >= thresh

    def summary(
        self,
        *,
        online_names: Optional[Iterable[str]] = None,
        instant_idle_names: Optional[Iterable[str]] = None,
        settings: Optional[dict] = None,
    ) -> dict:
        settings = settings or idle_window_settings()
        online = [
            str(n)
            for n in (online_names if online_names is not None else self._slaves.keys())
            if n
        ]
        if instant_idle_names is None:
            instant_idle = {
                n for n, st in self._slaves.items() if st.last_idle
            }
        else:
            instant_idle = {str(n) for n in instant_idle_names if n}

        fracs: List[float] = []
        sustained = 0
        rows = []
        for name in online:
            frac = self.idle_frac(name)
            if frac is not None:
                fracs.append(frac)
            is_sust = self.is_sustained_idle(name, settings)
            if is_sust:
                sustained += 1
            st = self._slaves.get(name)
            rows.append(
                {
                    "slave_name": name,
                    "instant_idle": name in instant_idle,
                    "sustained_idle": is_sust,
                    "idle_frac_window": None if frac is None else round(frac, 3),
                    "observed_ms": int(st.observed_ms) if st else 0,
                    "continuous_idle_ms": int(st.continuous_idle_ms) if st else 0,
                }
            )

        mean_frac = round(sum(fracs) / len(fracs), 3) if fracs else None
        return {
            "enabled": bool(settings.get("enabled", True)),
            "window_ms": int(settings.get("window_ms") or 0),
            "frac_threshold": float(settings.get("frac_threshold") or 0.5),
            "min_observed_ms": int(settings.get("min_observed_ms") or 0),
            "min_continuous_ms": int(settings.get("min_continuous_ms") or 0),
            "online": len(online),
            "instant_idle": len(instant_idle & set(online)),
            "sustained_idle": sustained,
            "mean_idle_frac_window": mean_frac,
            "rows": rows,
        }


FLEET_IDLE_TRACKER = IdleWindowTracker()
CPU_IDLE_TRACKER = IdleWindowTracker()
