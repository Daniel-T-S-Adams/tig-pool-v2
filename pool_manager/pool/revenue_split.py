"""
Revenue-attributed member pay (PAY_MODE=revenue) — shadow first.

Why
---
Effort mode prices one GPU-second and one CPU-second identically. TIG does
not: what the pool earns from each challenge depends on that challenge's
emission slice and on how many qualifiers the pool holds there, and both
move every round. Revenue mode pays each nonce what its challenge actually
earned:

    pot_c   = pool round TIG × attribution_c            (from TIG block data)
    share_w = Σ_c pot_frac_c × credits(w, c) / credits(pool, c)

credits are the same effort credits as PAY_MODE=effort (nonces × fleet
seconds/nonce per track), so *within* a challenge nothing changes; only the
weight *between* challenges is now TIG's number instead of a guess.

Attribution per block, from /get-opow and /get-challenges:

    factor_c = Σ_t pool_qualifiers[c][t] × legacy_mult[c][t] / Σ_t total_qualifiers[c][t]
    attribution_c = factor_c / Σ_c factor_c

That is the per-challenge term of TIG's influence formula; the imbalance
penalty scales all challenges together so it cancels in the fractions.

Insurance
---------
A slave told to work a challenge where the pool earns nothing would earn
nothing under pure attribution. PAY_EFFORT_BLEND (default 0.2) mixes that
much pure effort back in. With POOL_FEE=0 this is funded by the other
members, not the operator — keep it small.

Sampling every REVENUE_SAMPLE_INTERVAL_S seconds is enough: only the
*proportions* are used, so gaps do not bias the split.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable, Mapping

from . import work_credits

# `database` is imported inside the functions that need it so the pure math
# above the fold stays importable without psycopg2 (tools/test_revenue_split.py).

logger = logging.getLogger("pool.revenue_split")

SAMPLE_INTERVAL_S = float(os.environ.get("REVENUE_SAMPLE_INTERVAL_S", "300"))
SAMPLE_KEEP_DAYS = int(os.environ.get("REVENUE_SAMPLE_KEEP_DAYS", "60"))
DEFAULT_EFFORT_BLEND = 0.2
MAX_BLOCKS_COVERED = 60  # a long outage must not let one sample dominate
_REPORT_TTL_S = 60.0
_report_cache: dict[str, Any] = {"ts": 0.0, "key": None, "data": None}
_last_sample_mono = 0.0
_schema_ready = False

DEFAULT_API_URL = "https://mainnet-api.tig.foundation"
_HTTP_HEADERS = {"User-Agent": "innopool-manager/revenue", "Accept": "application/json"}


# ── pure helpers (unit-tested, no DB / network) ──────────────────────────────

def effort_blend(override: float | None = None) -> float:
    raw = override if override is not None else os.environ.get("PAY_EFFORT_BLEND", DEFAULT_EFFORT_BLEND)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_EFFORT_BLEND
    return min(1.0, max(0.0, value))


def attribute_block(pool_block_data: Mapping[str, Any], challenges: Iterable[Mapping[str, Any]]) -> dict[str, dict]:
    """Per-challenge factor for one block.

    Returns {challenge_name: {"factor", "attribution", "pool_q", "total_q", "type"}}
    for every active challenge (zero factor where the pool has no qualifiers).
    """
    pool_q_by = (pool_block_data or {}).get("num_qualifiers_by_challenge_by_track") or {}
    mult_by = (pool_block_data or {}).get("legacy_multiplier_by_challenge_by_track") or {}
    out: dict[str, dict] = {}
    for ch in challenges or []:
        cid = str(ch.get("id") or "")
        cfg = ch.get("config") or {}
        name = str(cfg.get("name") or cid)
        if not name:
            continue
        totals = ((ch.get("block_data") or {}).get("num_qualifiers_by_track") or {})
        total_q = sum(int(v or 0) for v in totals.values())
        pool_tracks = pool_q_by.get(cid) or {}
        mults = mult_by.get(cid) or {}
        weighted = 0.0
        pool_q = 0
        for track, q in pool_tracks.items():
            q = int(q or 0)
            pool_q += q
            try:
                m = float(mults.get(track, 1.0) or 1.0)
            except (TypeError, ValueError):
                m = 1.0
            weighted += q * m
        factor = (weighted / total_q) if total_q > 0 else 0.0
        out[name] = {
            "id": cid,
            "type": str(cfg.get("type") or ("gpu" if work_credits.is_gpu_challenge(name) else "cpu")),
            "factor": round(factor, 8),
            "pool_q": pool_q,
            "total_q": total_q,
        }
    total_factor = sum(v["factor"] for v in out.values())
    for v in out.values():
        v["attribution"] = round(v["factor"] / total_factor, 8) if total_factor > 0 else 0.0
    return out


def pot_fractions(samples: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Round pot fraction per challenge from block samples.

    Each sample carries reward_tig, blocks_covered and attribution per
    challenge; weight = reward × blocks so a sample that stands for more
    blocks (or more TIG) counts more. Sums to 1.0 over challenges.
    """
    pots: dict[str, float] = {}
    total = 0.0
    for s in samples or []:
        try:
            reward = float(s.get("reward_tig") or 0)
            blocks = max(1, int(s.get("blocks_covered") or 1))
        except (TypeError, ValueError):
            continue
        weight = reward * blocks
        if weight <= 0:
            continue
        attr = s.get("attribution") or {}
        if isinstance(attr, str):
            try:
                attr = json.loads(attr)
            except ValueError:
                continue
        for name, v in attr.items():
            frac = float((v or {}).get("attribution") or 0) if isinstance(v, Mapping) else float(v or 0)
            if frac > 0:
                pots[str(name)] = pots.get(str(name), 0.0) + weight * frac
                total += weight * frac
    if total <= 0:
        return {}
    return {k: v / total for k, v in pots.items()}


def revenue_shares(
    credits: Mapping[tuple[str, str], float],
    pots: Mapping[str, float],
) -> dict[str, float]:
    """(owner, challenge) -> credits, plus pot fraction per challenge -> owner share.

    Challenges with pool work but no pot earn nothing here (the blend covers
    them). Pot fractions on challenges nobody worked are dropped and the
    rest renormalised, so the shares sum to 1.0.
    """
    pool_by_chal: dict[str, float] = {}
    for (owner, chal), amt in credits.items():
        if amt > 0:
            pool_by_chal[chal] = pool_by_chal.get(chal, 0.0) + float(amt)
    usable = {c: f for c, f in pots.items() if f > 0 and pool_by_chal.get(c, 0.0) > 0}
    scale = sum(usable.values())
    if scale <= 0:
        return {}
    out: dict[str, float] = {}
    for (owner, chal), amt in credits.items():
        f = usable.get(chal)
        if not f or amt <= 0:
            continue
        out[owner] = out.get(owner, 0.0) + (f / scale) * (float(amt) / pool_by_chal[chal])
    return out


def blend_shares(revenue: Mapping[str, float], effort: Mapping[str, float], beta: float) -> dict[str, float]:
    """(1-β)·revenue + β·effort, each side normalised first. β=1 is pure effort."""
    beta = min(1.0, max(0.0, float(beta)))

    def _norm(m: Mapping[str, float]) -> dict[str, float]:
        t = sum(v for v in m.values() if v > 0)
        return {k: v / t for k, v in m.items() if v > 0} if t > 0 else {}

    r, e = _norm(revenue), _norm(effort)
    if not r:
        return e
    if not e:
        return r
    out: dict[str, float] = {}
    for k in set(r) | set(e):
        out[k] = (1.0 - beta) * r.get(k, 0.0) + beta * e.get(k, 0.0)
    return out


# ── schema + sampler ─────────────────────────────────────────────────────────

def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    from . import database as db

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS challenge_reward_samples (
            block_height BIGINT PRIMARY KEY,
            round_id INTEGER,
            sampled_at_ms BIGINT NOT NULL,
            blocks_covered INTEGER NOT NULL DEFAULT 1,
            reward_tig DOUBLE PRECISION NOT NULL,
            influence DOUBLE PRECISION,
            attribution JSONB NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_crs_round ON challenge_reward_samples(round_id)")
    _schema_ready = True


def _tig_config() -> tuple[str, str]:
    from . import database as db

    row = db.fetch_one("SELECT config FROM config LIMIT 1")
    cfg = (row or {}).get("config") or {}
    api_url = str(cfg.get("api_url") or DEFAULT_API_URL).rstrip("/")
    player_id = str(cfg.get("player_id") or "").lower()
    return api_url, player_id


def _fetch_json(url: str, timeout: float = 10.0):
    import requests

    resp = requests.get(url, headers=_HTTP_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def sample_once() -> dict | None:
    """Fetch one block's attribution and store it. Returns the stored row."""
    from . import database as db

    ensure_schema()
    api_url, player_id = _tig_config()
    if not player_id:
        logger.debug("revenue sampler: no player_id in config yet")
        return None
    block = (_fetch_json(f"{api_url}/get-block") or {}).get("block") or {}
    block_id = block.get("id")
    details = block.get("details") or {}
    height = int(details.get("height") or block.get("height") or 0)
    round_id = details.get("round")
    if not block_id or height <= 0:
        return None
    if db.fetch_one("SELECT 1 FROM challenge_reward_samples WHERE block_height = %s", (height,)):
        return None  # same block as last tick

    opow = _fetch_json(f"{api_url}/get-opow?block_id={block_id}")
    entries = opow.get("opow") if isinstance(opow, dict) and "opow" in opow else opow
    mine = None
    if isinstance(entries, list):
        mine = next((e for e in entries if str(e.get("player_id") or "").lower() == player_id), None)
    elif isinstance(entries, dict):
        mine = entries.get(player_id)
    bd = (mine or {}).get("block_data") or {}
    reward_tig = int(bd.get("reward") or 0) / 1e18
    influence = bd.get("influence")

    ch = _fetch_json(f"{api_url}/get-challenges?block_id={block_id}")
    challenges = ch.get("challenges") if isinstance(ch, dict) else ch
    attribution = attribute_block(bd, challenges or [])

    prev = db.fetch_one(
        "SELECT block_height FROM challenge_reward_samples ORDER BY block_height DESC LIMIT 1"
    )
    blocks_covered = 1
    if prev and int(prev["block_height"]) < height:
        blocks_covered = min(MAX_BLOCKS_COVERED, height - int(prev["block_height"]))
    db.execute(
        """
        INSERT INTO challenge_reward_samples
            (block_height, round_id, sampled_at_ms, blocks_covered, reward_tig, influence, attribution)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (block_height) DO NOTHING
        """,
        (
            height,
            int(round_id) if round_id is not None else None,
            int(time.time() * 1000),
            blocks_covered,
            reward_tig,
            float(influence) if influence is not None else None,
            json.dumps(attribution, separators=(",", ":")),
        ),
    )
    db.execute(
        "DELETE FROM challenge_reward_samples WHERE sampled_at_ms < %s",
        (int(time.time() * 1000) - SAMPLE_KEEP_DAYS * 86400 * 1000,),
    )
    logger.info(
        "revenue sample h=%s round=%s reward=%.4f TIG/block attribution=%s",
        height, round_id, reward_tig,
        {k: round(v["attribution"], 3) for k, v in attribution.items() if v["attribution"] > 0},
    )
    return {"block_height": height, "round_id": round_id, "reward_tig": reward_tig, "attribution": attribution}


def maybe_sample() -> None:
    """Call from the background loop; no-op between intervals."""
    global _last_sample_mono
    mono = time.monotonic()
    if mono - _last_sample_mono < SAMPLE_INTERVAL_S:
        return
    _last_sample_mono = mono
    try:
        sample_once()
    except Exception as exc:
        logger.warning("revenue sample failed: %s", exc)


# ── round pots + shares ──────────────────────────────────────────────────────

def _current_round_id() -> int | None:
    from . import database as db

    raw = db.get_setting("current_round_id", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def round_samples(round_id: int | None, since_ms: int | None) -> list[dict]:
    from . import database as db

    ensure_schema()
    if round_id is not None:
        rows = db.fetch_all(
            "SELECT * FROM challenge_reward_samples WHERE round_id = %s ORDER BY block_height",
            (int(round_id),),
        )
        if rows:
            return [dict(r) for r in rows]
    if since_ms is not None:
        rows = db.fetch_all(
            "SELECT * FROM challenge_reward_samples WHERE sampled_at_ms >= %s ORDER BY block_height",
            (int(since_ms),),
        )
        return [dict(r) for r in rows]
    return []


def _credits_by_wallet_challenge(since_ms: int | None) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], float], dict[str, str]]:
    """Effort credits per (wallet, challenge) and per (slave, challenge)."""
    from . import database as db

    weights = work_credits.fetch_weight_table()
    sql, params = work_credits._work_sql(since_ms)
    rows = db.fetch_all(sql, params) or []
    members = db.fetch_all("SELECT slave_name, wallet_address FROM pool_members WHERE active = true") or []
    slave_wallet = {r["slave_name"]: r["wallet_address"] for r in members}
    by_wallet: dict[tuple[str, str], float] = {}
    by_slave: dict[tuple[str, str], float] = {}
    for row in rows:
        slave = str(row.get("slave") or "")
        wallet = slave_wallet.get(slave)
        if not wallet:
            continue
        challenge = str(row.get("challenge") or "")
        junk = bool(row.get("junk"))
        scored = work_credits.score_root_group(
            nonces=float(row.get("nonces") or 0),
            challenge=challenge,
            track_id=str(row.get("track_id") or ""),
            weights=weights,
            runtime_ms=float(row.get("runtime_ms") or 0),
            stopped=junk,
            proof_submitted=not junk,
        )
        cr = float(scored["credits_weight"])
        if cr <= 0:
            continue
        by_wallet[(wallet, challenge)] = by_wallet.get((wallet, challenge), 0.0) + cr
        by_slave[(slave, challenge)] = by_slave.get((slave, challenge), 0.0) + cr
    return by_wallet, by_slave, slave_wallet


def _effort_totals(credits: Mapping[tuple[str, str], float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for (owner, _), amt in credits.items():
        out[owner] = out.get(owner, 0.0) + amt
    return out


def wallet_shares(since_ms: int | None, *, scale: float = 1.0, blend: float | None = None) -> dict[str, float]:
    """Live allocation for PAY_MODE=revenue. Empty dict => caller falls back."""
    pots = pot_fractions(round_samples(_current_round_id(), since_ms))
    if not pots:
        return {}
    by_wallet, _, _ = _credits_by_wallet_challenge(since_ms)
    rev = revenue_shares(by_wallet, pots)
    final = blend_shares(rev, _effort_totals(by_wallet), effort_blend(blend))
    return work_credits.fractions_from_amounts(final, scale)


def build_report(*, pool_fee: float, round_start_ms: int | None, top_n: int = 25, force: bool = False, blend: float | None = None) -> dict:
    """Shadow comparison: effort vs revenue vs blended, per challenge / wallet / slave."""
    key = (round_start_ms, blend)
    now = time.time()
    if not force and _report_cache["data"] is not None and _report_cache["key"] == key and now - _report_cache["ts"] < _REPORT_TTL_S:
        return _report_cache["data"]

    member_share = max(0.0, 1.0 - float(pool_fee or 0))
    beta = effort_blend(blend)
    round_id = _current_round_id()
    samples = round_samples(round_id, round_start_ms)
    pots = pot_fractions(samples)
    by_wallet, by_slave, slave_wallet = _credits_by_wallet_challenge(round_start_ms)

    # per-challenge: pool effort share vs revenue share
    pool_chal: dict[str, float] = {}
    for (_, chal), amt in by_wallet.items():
        pool_chal[chal] = pool_chal.get(chal, 0.0) + amt
    total_credits = sum(pool_chal.values())
    latest_attr = (samples[-1].get("attribution") if samples else {}) or {}
    if isinstance(latest_attr, str):
        latest_attr = json.loads(latest_attr)
    challenges = []
    for name in sorted(set(pool_chal) | set(pots)):
        eff = pool_chal.get(name, 0.0) / total_credits if total_credits > 0 else 0.0
        rev = pots.get(name, 0.0)
        la = latest_attr.get(name) or {}
        challenges.append(
            {
                "challenge": name,
                "type": la.get("type") or ("gpu" if work_credits.is_gpu_challenge(name) else "cpu"),
                "effort_pct": round(100 * eff, 2),
                "revenue_pct": round(100 * rev, 2),
                # >1: this challenge pays more per effort-hour than the pool average
                "value_index": round(rev / eff, 3) if eff > 0 else None,
                "pool_q": la.get("pool_q"),
                "total_q": la.get("total_q"),
                "effort_hours": round(pool_chal.get(name, 0.0) / 3600.0, 2),
            }
        )
    fam = {"cpu": {"effort": 0.0, "revenue": 0.0}, "gpu": {"effort": 0.0, "revenue": 0.0}}
    for c in challenges:
        f = fam["gpu" if c["type"] == "gpu" else "cpu"]
        f["effort"] += c["effort_pct"]
        f["revenue"] += c["revenue_pct"]
    family = {k: {"effort_pct": round(v["effort"], 2), "revenue_pct": round(v["revenue"], 2)} for k, v in fam.items()}

    def _table(credits: Mapping[tuple[str, str], float], label: str) -> list[dict]:
        eff_amt = _effort_totals(credits)
        eff = work_credits.fractions_from_amounts(eff_amt, member_share)
        rev = work_credits.fractions_from_amounts(revenue_shares(credits, pots), member_share) if pots else {}
        bl = work_credits.fractions_from_amounts(blend_shares(revenue_shares(credits, pots), eff_amt, beta), member_share) if pots else eff
        rows = []
        for owner in set(eff) | set(rev) | set(bl):
            e, r, b = eff.get(owner, 0.0), rev.get(owner, 0.0), bl.get(owner, 0.0)
            row = {
                label: owner,
                "effort_share": round(e, 6),
                "revenue_share": round(r, 6),
                "blended_share": round(b, 6),
                "delta": round(b - e, 6),
                "delta_pct_of_effort": round(100 * (b - e) / e, 1) if e > 0 else None,
                "credit_hours": round(eff_amt.get(owner, 0.0) / 3600.0, 2),
            }
            if label == "slave_name":
                row["wallet_address"] = slave_wallet.get(owner, "")
                row["profile"] = "gpu" if str(owner).startswith("pool-gpu-") else "cpu"
            rows.append(row)
        rows.sort(key=lambda r: -abs(r["delta"]))
        return rows

    wallets = _table(by_wallet, "wallet_address")
    slaves = _table(by_slave, "slave_name")
    blocks_covered = sum(max(1, int(s.get("blocks_covered") or 1)) for s in samples)
    live = work_credits.pay_mode()
    notes = []
    if not samples:
        notes.append("no reward samples yet for this round — revenue split falls back to effort; sampler runs every %ds" % int(SAMPLE_INTERVAL_S))
    else:
        g, c = family["gpu"], family["cpu"]
        if g["effort_pct"] > 0:
            notes.append(f"GPU work is {g['effort_pct']:.1f}% of effort and earns {g['revenue_pct']:.1f}% of TIG (CPU {c['effort_pct']:.1f}% / {c['revenue_pct']:.1f}%)")
        zero = [x["challenge"] for x in challenges if x["effort_pct"] > 0 and x["revenue_pct"] == 0]
        if zero:
            notes.append(f"pool works but earns nothing on: {', '.join(zero)} — only the {beta:.0%} effort blend pays that work")
        big = [w for w in wallets if w["delta_pct_of_effort"] is not None and abs(w["delta_pct_of_effort"]) >= 10]
        if big:
            notes.append(f"{len(big)} wallet(s) would move by 10%+ vs effort pay")
    payload = {
        "mode": live,
        "live_payout": live,
        "shadow": live != "revenue",
        "blend": beta,
        "member_share": member_share,
        "round_id": round_id,
        "round_start_ms": round_start_ms,
        "samples": len(samples),
        "blocks_covered": blocks_covered,
        "latest_block": int(samples[-1]["block_height"]) if samples else None,
        "latest_reward_tig_per_block": round(float(samples[-1]["reward_tig"]), 6) if samples else None,
        "family": family,
        "challenges": challenges,
        "wallets": wallets[: max(1, int(top_n))],
        "slaves": slaves[: max(1, int(top_n))],
        "wallets_total": len(wallets),
        "notes": notes,
        "note": (
            "Revenue mode pays each challenge's effort from the TIG that challenge actually earned "
            "(TIG qualifier data per block), blended with PAY_EFFORT_BLEND of pure effort. "
            "Set PAY_MODE=revenue to make this the live /set-coinbase split."
        ),
    }
    _report_cache.update(ts=now, key=key, data=payload)
    return payload
