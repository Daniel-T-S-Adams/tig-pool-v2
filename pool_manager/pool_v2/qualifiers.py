"""Attribute saved qualifying credit to immutable benchmark owners, once."""

from collections import defaultdict
from fractions import Fraction

from psycopg2.extras import execute_values

from .block_observer import BlockStore
from .database import lock
from .ledger import fingerprint
from .protocol import ProtocolDataError, equal_bundle_credit


RULE = "equal-cutoff-bundles-v1"


def credit_block(database, block_id, player_id):
    _, snapshot = BlockStore(database).read(block_id)
    owned_ids = {identity for identity, row in snapshot.precommits.items()
                 if row["settings"]["player_id"] == player_id}
    credits = equal_bundle_credit(snapshot)
    by_benchmark = defaultdict(Fraction)
    for (benchmark_id, _), credit in credits.items():
        if benchmark_id in owned_ids:
            by_benchmark[benchmark_id] += credit
    with database.transaction() as cursor:
        lock(cursor, "credit:" + block_id)
        cursor.execute("SELECT benchmark_id,member_id,handed_over_at FROM reservations WHERE benchmark_id=ANY(%s)",
                       (sorted(owned_ids),))
        owners = {row["benchmark_id"]: row for row in cursor.fetchall()}
        if owners.keys() != owned_ids or any(row["handed_over_at"] is None for row in owners.values()):
            raise ProtocolDataError("pool benchmark ownership or confirmed handover is missing")
        owner_digest = fingerprint({key: str(row["member_id"]) for key, row in owners.items()})
        total = sum(by_benchmark.values(), Fraction())
        expected = sum(count for (player, _, _, _), count in snapshot.qualifiers.items() if player == player_id)
        if total != expected:
            raise ProtocolDataError("member credit does not reconcile to pool qualifying totals")
        cursor.execute("SELECT * FROM credited_blocks WHERE block_id=%s", (block_id,))
        previous = cursor.fetchone()
        if previous:
            if (previous["player_id"], previous["rule"], previous["ownership_digest"],
                Fraction(int(previous["total_num"]), int(previous["total_den"]))) != (player_id, RULE, owner_digest, total):
                raise ProtocolDataError("replayed ownership or credit differs from the recorded block")
            return total
        cursor.execute("""INSERT INTO credited_blocks(block_id,player_id,rule,ownership_digest,total_num,total_den)
            VALUES (%s,%s,%s,%s,%s,%s)""", (block_id, player_id, RULE, owner_digest, total.numerator, total.denominator))
        if by_benchmark:
            execute_values(cursor, """INSERT INTO benchmark_credits
                (block_id,benchmark_id,member_id,numerator,denominator) VALUES %s""",
                [(block_id, key, owners[key]["member_id"], value.numerator, value.denominator)
                 for key, value in sorted(by_benchmark.items())])
        return total


def round_credits(database, round_number):
    if not BlockStore(database).round_coverage(round_number):
        raise ProtocolDataError("reward round has incomplete block or credit coverage")
    result = defaultdict(Fraction)
    with database.transaction() as cursor:
        cursor.execute("""SELECT c.member_id,c.numerator,c.denominator FROM benchmark_credits c
            JOIN observed_blocks b ON b.id=c.block_id WHERE b.round=%s""", (round_number,))
        for row in cursor.fetchall():
            result[str(row["member_id"])] += Fraction(int(row["numerator"]), int(row["denominator"]))
    return dict(result)
