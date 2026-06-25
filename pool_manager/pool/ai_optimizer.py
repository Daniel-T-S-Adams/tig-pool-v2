"""
Read-only AI pool optimizer.

The AI optimizer asks DeepSeek for a structured recommendation using stable
operator context plus live autopilot telemetry. It never applies changes itself;
recommendations are stored for review and future guarded execution.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

from pool import autopilot
from pool import database as db

logger = logging.getLogger("pool.ai_optimizer")

AI_OPTIMIZER_MODE = os.environ.get("AI_OPTIMIZER_MODE", "off").lower()
AI_OPTIMIZER_INTERVAL_S = int(os.environ.get("AI_OPTIMIZER_INTERVAL_S", "600"))
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_URL = os.environ.get(
    "DEEPSEEK_API_URL",
    "https://api.deepseek.com/chat/completions",
)
AI_CONTEXT_PATH = os.environ.get("AI_CONTEXT_PATH", "/app/docs/ai_pool_operator_context.md")
AI_OPTIMIZER_TIMEOUT_S = int(os.environ.get("AI_OPTIMIZER_TIMEOUT_S", "90"))
AI_OPTIMIZER_HISTORY_LIMIT = int(os.environ.get("AI_OPTIMIZER_HISTORY_LIMIT", "8"))
AI_OPTIMIZER_MAX_TOKENS = int(os.environ.get("AI_OPTIMIZER_MAX_TOKENS", "2500"))

_last_run_ts = 0.0
_decision_table_ready = False


def _ensure_decision_table():
    global _decision_table_ready
    if _decision_table_ready:
        return
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_optimizer_decisions (
            id BIGSERIAL PRIMARY KEY,
            mode TEXT NOT NULL,
            generated_at_ms BIGINT NOT NULL,
            model TEXT,
            status TEXT NOT NULL,
            decision_category TEXT,
            confidence DOUBLE PRECISION,
            summary TEXT,
            recommendation JSONB NOT NULL DEFAULT '{}'::JSONB,
            raw_response TEXT,
            prompt_context JSONB NOT NULL DEFAULT '{}'::JSONB,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    _decision_table_ready = True


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _load_context() -> str:
    path = Path(AI_CONTEXT_PATH)
    if path.exists():
        return path.read_text(encoding="utf-8")

    fallback = Path(__file__).resolve().parents[2] / "docs" / "ai_pool_operator_context.md"
    if fallback.exists():
        return fallback.read_text(encoding="utf-8")

    raise FileNotFoundError(f"AI context document not found: {AI_CONTEXT_PATH}")


def _recent_ai_decisions() -> list[dict]:
    _ensure_decision_table()
    rows = db.fetch_all(
        """
        SELECT
            id,
            created_at,
            status,
            decision_category,
            confidence,
            summary,
            recommendation,
            error
        FROM ai_optimizer_decisions
        ORDER BY id DESC
        LIMIT %s
        """,
        (AI_OPTIMIZER_HISTORY_LIMIT,),
    )
    return [_json_safe(dict(r)) for r in rows]


def _recent_autopilot_decisions() -> list[dict]:
    try:
        rows = db.fetch_all(
            """
            SELECT
                id,
                created_at,
                reason,
                applied,
                clean_windows,
                changes
            FROM autopilot_decisions
            ORDER BY id DESC
            LIMIT %s
            """,
            (AI_OPTIMIZER_HISTORY_LIMIT,),
        )
        return [_json_safe(dict(r)) for r in rows]
    except Exception as exc:
        logger.info("recent autopilot decisions unavailable: %s", exc)
        return []


def _build_prompt_payload(report: dict) -> dict:
    return {
        "generated_at_ms": int(time.time() * 1000),
        "mode": AI_OPTIMIZER_MODE,
        "autopilot_report": report,
        "recent_autopilot_decisions": _recent_autopilot_decisions(),
        "recent_ai_optimizer_decisions": _recent_ai_decisions(),
        "instructions": {
            "output": "Return strict JSON only. Follow the schema in the context document.",
            "apply_policy": "Read-only analysis. Do not claim any change has been applied.",
            "if_uncertain": "Use request_more_data or observe_only.",
        },
    }


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _parse_model_json(text: str) -> dict:
    parsed = json.loads(_strip_json_fences(text))
    if not isinstance(parsed, dict):
        raise ValueError("model response JSON must be an object")
    parsed.setdefault("schema_version", 1)
    parsed.setdefault("decision_category", "observe_only")
    parsed.setdefault("summary", "")
    parsed.setdefault("confidence", 0.0)
    parsed.setdefault("recommended_actions", [])
    parsed.setdefault("requires_human_approval", False)
    return parsed


def _call_deepseek(context_doc: str, payload: dict) -> tuple[dict, str]:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")

    messages = [
        {
            "role": "system",
            "content": context_doc,
        },
        {
            "role": "user",
            "content": (
                "Analyze this live InnoPool telemetry and return strict JSON only.\n\n"
                + json.dumps(payload, sort_keys=True)
            ),
        },
    ]
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": AI_OPTIMIZER_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(
        DEEPSEEK_API_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=AI_OPTIMIZER_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data["choices"][0]["message"]["content"]
    return _parse_model_json(raw), raw


def _save_decision(
    *,
    status: str,
    recommendation: dict | None,
    prompt_context: dict,
    raw_response: str = "",
    error: str = "",
):
    _ensure_decision_table()
    recommendation = recommendation or {}
    db.execute(
        """
        INSERT INTO ai_optimizer_decisions (
            mode,
            generated_at_ms,
            model,
            status,
            decision_category,
            confidence,
            summary,
            recommendation,
            raw_response,
            prompt_context,
            error
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::JSONB, %s, %s::JSONB, %s)
        """,
        (
            AI_OPTIMIZER_MODE,
            int(time.time() * 1000),
            DEEPSEEK_MODEL,
            status,
            recommendation.get("decision_category"),
            float(recommendation.get("confidence") or 0),
            recommendation.get("summary"),
            json.dumps(recommendation),
            raw_response,
            json.dumps(prompt_context),
            error,
        ),
    )


def run_once(force: bool = False) -> dict:
    """Run one read-only AI recommendation cycle and store the result."""
    _ensure_decision_table()
    if AI_OPTIMIZER_MODE not in {"report", "off"}:
        return {"status": "skipped", "reason": f"unsupported_mode:{AI_OPTIMIZER_MODE}"}
    if AI_OPTIMIZER_MODE == "off" and not force:
        return {"status": "skipped", "reason": "mode_off"}

    prompt_context: dict = {}
    try:
        context_doc = _load_context()
        report = autopilot.build_report()
        prompt_context = _build_prompt_payload(report)
        recommendation, raw = _call_deepseek(context_doc, prompt_context)
        _save_decision(
            status="ok",
            recommendation=recommendation,
            prompt_context={
                "generated_at_ms": prompt_context["generated_at_ms"],
                "autopilot_generated_at_ms": report.get("generated_at_ms"),
                "recent_autopilot_decisions": len(prompt_context["recent_autopilot_decisions"]),
                "recent_ai_optimizer_decisions": len(prompt_context["recent_ai_optimizer_decisions"]),
            },
            raw_response=raw,
        )
        logger.info(
            "ai_optimizer status=ok category=%s confidence=%s summary=%s",
            recommendation.get("decision_category"),
            recommendation.get("confidence"),
            recommendation.get("summary"),
        )
        return {"status": "ok", "recommendation": recommendation}
    except Exception as exc:
        error = str(exc)
        _save_decision(
            status="error",
            recommendation={"decision_category": "observe_only", "summary": "AI optimizer failed."},
            prompt_context=prompt_context,
            error=error,
        )
        logger.error("ai_optimizer error: %s", error)
        return {"status": "error", "error": error}


def maybe_run():
    global _last_run_ts
    if AI_OPTIMIZER_MODE != "report":
        return None
    now = time.time()
    if now - _last_run_ts < AI_OPTIMIZER_INTERVAL_S:
        return None
    _last_run_ts = now
    return run_once()


def latest(limit: int = 10) -> list[dict]:
    _ensure_decision_table()
    rows = db.fetch_all(
        """
        SELECT
            id,
            mode,
            created_at,
            model,
            status,
            decision_category,
            confidence,
            summary,
            recommendation,
            error
        FROM ai_optimizer_decisions
        ORDER BY id DESC
        LIMIT %s
        """,
        (max(1, min(50, int(limit or 10))),),
    )
    return [_json_safe(dict(r)) for r in rows]
