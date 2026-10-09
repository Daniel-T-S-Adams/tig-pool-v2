"""Public reporting evidence, independently captured before database access."""

from datetime import datetime, timezone
import time

from . import reports
from .protocol import ProtocolDataError, validate_reports, _integer
from .observation import canonical_json
import hashlib


FINAL_CACHE_GRACE_SECONDS = 65  # pinned get-reports backend cache TTL: 60 seconds


def capture(client, reporting_round, *, player_id=None, challenge_id=None, max_block_age=180,
            benchmark_ids=None, known_report_ids=(), final_not_before=None):
    """One current-block bracket per public request; partial failures are evidence."""
    if bool(player_id) != bool(challenge_id):
        raise ValueError("reportable indices require both a player and a challenge")
    result = {"reporting_round": reporting_round, "kind": "index" if player_id else "reports"}
    if player_id:
        result.update(player_id=player_id, challenge_id=challenge_id)
    error = None
    try:
        result["start"] = client.get("/get-block")
        head = result["start"]["block"]
        if player_id:
            result["payload"] = client.get("/get-reportable-benchmark-ids", {
                "round": reporting_round, "player_id": player_id, "challenge_id": challenge_id})
        else:
            result["payload"] = client.get("/get-reports", {"round": reporting_round})
        result["end"] = client.get("/get-block")
        if result["end"]["block"]["id"] != head["id"]:
            raise ProtocolDataError("report request crossed a block boundary; retry it")
        age = time.time() - head["details"]["timestamp"]
        if not -5 <= age <= max_block_age:
            raise ProtocolDataError("report observation has a stale or future block anchor")
        if not player_id and benchmark_ids is not None:
            period = _integer(head['config']['reports']['submission_period'], 'report submission period')
            result['report_submission_period'] = period
            payload = result["payload"]
            values = validate_reports(payload)
            result["source_payload_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
            result["scope_benchmark_ids"] = sorted(benchmark_ids)
            wanted = {identity for identity, row in values.items()
                      if row["benchmark_id"] in benchmark_ids or identity in known_report_ids}
            result["payload"] = {"reports": [row for row in payload["reports"] if row["id"] in wanted],
                "arbitrations": [row for row in payload["arbitrations"] if row["report_id"] in wanted]}
            if head['details']['round'] < reporting_round + period + 1:
                raise ProtocolDataError('the reporting round submission period is still open')
            if final_not_before is not None:
                response = next(row for row in reversed(client.records) if row['path'] == '/get-reports')
                edge_age = int(response.get('response_headers', {}).get('age', '0'))
                started = datetime.fromisoformat(response['started_at']).timestamp()
                if edge_age < 0 or started - edge_age < final_not_before:
                    raise ProtocolDataError('final report response may predate the closed reporting window cache grace')
                result['final_not_before_utc'] = datetime.fromtimestamp(final_not_before, timezone.utc).isoformat()
            if any(values[identity]["result"] is None for identity in wanted
                   if values[identity]["benchmark_id"] in benchmark_ids):
                raise ProtocolDataError("owned report arbitration is still pending; retry final evidence")
    except Exception as failure:
        error = type(failure).__name__ + ": " + str(failure)
    metadata = {"captured_at": datetime.now(timezone.utc).isoformat(), "requests": client.records}
    for name in ("start", "end"):
        if name in result:
            block = result[name]["block"]
            result[name] = {"block": {"id": block["id"], "details": {
                key: block["details"][key] for key in ("height", "round", "timestamp")}}}
    return result, metadata, error


def record(database, capture, metadata, error=None):
    """Called by the spool drainer; wait for the independent block archive."""
    try:
        block_id = capture["start"]["block"]["id"]
    except (KeyError, TypeError) as failure:
        raise ProtocolDataError("report evidence is missing its block anchor") from failure
    # The block itself is already in the pool archive; keep only its identity.
    head = capture["start"]["block"]
    provenance = {**metadata, "start_block": {"id": block_id, "height": head["details"]["height"]},
                  "end_block_id": (capture.get("end") or {}).get("block", {}).get("id")}
    for key in ("source_payload_sha256", "scope_benchmark_ids", "report_submission_period", 'final_not_before_utc'):
        if key in capture:
            provenance[key] = capture[key]
    if capture["kind"] == "reports" and "scope_benchmark_ids" in capture and error is None:
        provenance["final_for_pool"] = True
    arguments = (database, capture["reporting_round"], block_id)
    options = {"metadata": provenance, "error": error}
    if capture["kind"] == "index":
        return reports.record_index(*arguments, capture["player_id"], capture["challenge_id"],
                                    capture.get("payload", {}), **options)
    if capture["kind"] != "reports":
        raise ProtocolDataError("unknown reporting observation kind")
    return reports.record(*arguments, capture.get("payload", {}), **options)


def needed_queries(database, current_round, *, player_id, challenge_ids, extra_rounds=(), submission_period=2):
    """Observe positive mappings while open; fetch reports after X+2 closes.

    Explicit scopes remain the settlement adapter's responsibility. Unmapped
    benchmarks conservatively request their creation and following round;
    an empty index never establishes a mapping or permits settlement.
    """
    _integer(submission_period, 'report submission period')
    with database.transaction() as cursor:
        cursor.execute('''SELECT r.benchmark_id,r.creation_round,r.state,r.assignment,m.reporting_round
            FROM reservations r LEFT JOIN benchmark_reporting_rounds m ON m.benchmark_id=r.benchmark_id
            WHERE r.benchmark_id IS NOT NULL AND r.collateral_outcome IS NULL''')
        owned = cursor.fetchall()
        cursor.execute('SELECT creation_round,reporting_rounds FROM round_report_scopes')
        scopes = {row['creation_round']: row['reporting_rounds'] for row in cursor.fetchall()}
        cursor.execute('SELECT round FROM round_settlements')
        settled = {row['round'] for row in cursor.fetchall()}
        cursor.execute('SELECT round FROM round_earnings')
        reward_rounds = {row['round'] for row in cursor.fetchall()} - settled
        cursor.execute('SELECT benchmark_id FROM reservations WHERE benchmark_id IS NOT NULL')
        all_benchmark_ids = {row['benchmark_id'] for row in cursor.fetchall()}
        cursor.execute('''SELECT c.reporting_round,max(b.round) AS observed_round
            FROM report_captures c JOIN observed_blocks b ON b.id=c.block_id
            WHERE c.complete AND c.expired_at IS NULL AND c.metadata->>'final_for_pool'='true'
            GROUP BY c.reporting_round''')
        captured = {row['reporting_round']: row['observed_round'] for row in cursor.fetchall()}
        cursor.execute('SELECT round,min(captured_at) AS first_seen FROM observed_blocks GROUP BY round')
        boundaries = [(row['round'], row['first_seen'].timestamp()) for row in cursor.fetchall()]
    final = {}
    indices = set()
    mapping_ids = set()
    for creation in (set(scopes) | reward_rounds) - settled:
        if current_round >= creation + 3:
            for number in scopes.get(creation, [creation]):
                final[number] = max(final.get(number, 0), creation + 3, number + submission_period + 1)
    for row in owned:
        settings = (row['assignment'] or {}).get('settings', {})
        if settings.get('player_id') != player_id:
            continue
        creation = row['creation_round']
        if row['reporting_round'] is None and row['state'] in ('accepted', 'active', 'verification_failed'):
            challenge = settings.get('challenge_id')
            if challenge in challenge_ids:
                mapping_ids.add(row['benchmark_id'])
                indices.update((number, player_id, challenge)
                    for number in range(creation, current_round + 1))
        if current_round < creation + 3:
            continue
        rounds = scopes.get(creation) or ([row['reporting_round']] if row['reporting_round'] is not None
                                          else [creation, creation + 1])
        for number in rounds:
            final[number] = max(final.get(number, 0), creation + 3, number + submission_period + 1)
    # Operator-requested rounds are fetched explicitly, even without open work.
    for number in extra_rounds:
        final[number] = current_round
    not_before = {}
    for number, deadline in final.items():
        starts = [seen for observed_round, seen in boundaries if observed_round >= deadline]
        if starts:
            not_before[number] = min(starts) + FINAL_CACHE_GRACE_SECONDS
    report_rounds = sorted(number for number, deadline in final.items()
                           if current_round >= deadline and captured.get(number, 0) < deadline
                           and number in not_before and time.time() >= not_before[number])
    return {'reports': report_rounds, 'indices': sorted(indices),
            'benchmark_ids': all_benchmark_ids, 'mapping_benchmark_ids': mapping_ids,
            'deadlines': final, 'not_before': not_before}
