"""Software-enforced spending caps for protocol fees and custody gas.

The operator records a cap as an immutable row: a rolling window and, within
it, the most TIG the pool may commit to precommit fees and the most native
token it may reserve for custody sends, plus a ceiling per attempt. Ceilings
count in full from the moment they are committed, even when the actual fee was
lower, refunded or the send never happened, so a restart or refund can never
recycle budget. Without a recorded cap the pool commits no fees at all.
"""

from contextlib import nullcontext
from datetime import timedelta
import uuid

from .database import lock
from .money import Conflict, FundsError, units

UNSET = 'spending-cap-unset'
EXHAUSTED = 'spending-cap'
MIN_WINDOW, MAX_WINDOW = 3600, 31622400
FIELDS = ('tig_fee_window_units', 'tig_fee_attempt_units', 'native_fee_window_units', 'native_fee_attempt_units')


def current(cursor):
    cursor.execute('SELECT * FROM spending_caps ORDER BY created_at DESC, id DESC LIMIT 1')
    row = cursor.fetchone()
    return dict(row) if row else None


def set_cap(database, *, window_seconds, actor, reason, event_key, **limits):
    if set(limits) != set(FIELDS):
        raise FundsError('a spending cap needs window and per-attempt limits for TIG fees and native gas')
    for name in FIELDS:
        if units(limits[name]) >= 2**128:
            raise FundsError('spending cap limits are out of range')
    if type(window_seconds) is not int or not MIN_WINDOW <= window_seconds <= MAX_WINDOW:
        raise FundsError('spending cap window must be between one hour and one year, in seconds')
    for name, value in (('actor', actor), ('reason', reason), ('event_key', event_key)):
        if not isinstance(value, str) or not 1 <= len(value) <= (1000 if name == 'reason' else 128):
            raise FundsError('spending cap changes require an operator, reason and event key')
    if (limits['tig_fee_attempt_units'] > limits['tig_fee_window_units']
            or limits['native_fee_attempt_units'] > limits['native_fee_window_units']):
        raise FundsError('per-attempt limits cannot exceed their window limits')
    with database.transaction() as cursor:
        # Reservations and first precommit sends take this same budget fence.
        lock(cursor, 'operator:protocol-budget')
        cursor.execute('SELECT * FROM spending_caps WHERE event_key=%s', (event_key,))
        previous = cursor.fetchone()
        if previous:
            recorded = (previous['window_seconds'], previous['actor'], previous['reason'], *(int(previous[name]) for name in FIELDS))
            if recorded != (window_seconds, actor, reason, *(limits[name] for name in FIELDS)):
                raise Conflict('spending cap event key was reused with different inputs')
            return dict(previous)
        cursor.execute('''INSERT INTO spending_caps(id,event_key,window_seconds,tig_fee_window_units,tig_fee_attempt_units,
            native_fee_window_units,native_fee_attempt_units,actor,reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (uuid.uuid4(), event_key, window_seconds, *(limits[name] for name in FIELDS), actor, reason))
        return dict(cursor.fetchone())


def committed(cursor, cap, *, now=None, exclude=None):
    """Fee ceilings committed inside the window: sent precommits, pending reservations and custody sends."""
    if now is None:
        cursor.execute('SELECT clock_timestamp() AS now')
        now = cursor.fetchone()['now']
    since = now - timedelta(seconds=cap['window_seconds'])
    cursor.execute('''SELECT coalesce(sum(r.fee_limit),0) AS total FROM reservations r
        WHERE (%s::uuid IS NULL OR r.id<>%s::uuid) AND (
            EXISTS (SELECT 1 FROM reservation_events e WHERE e.reservation_id=r.id AND e.kind='potentially_sent'
                    AND e.created_at>%s AND e.created_at<=%s)
            OR (r.state='reserved' AND r.created_at>%s AND r.created_at<=%s))''',
        (exclude, exclude, since, now, since, now))
    tig = int(cursor.fetchone()['total'])
    cursor.execute('''SELECT coalesce(sum(fee_limit),0) AS total FROM (
        SELECT fee_limit,sent_at FROM withdrawal_attempts UNION ALL SELECT fee_limit,sent_at FROM protocol_topups) sends
        WHERE sent_at>%s AND sent_at<=%s''', (since, now))
    native = int(cursor.fetchone()['total'])
    return {'tig_fee_units': tig, 'native_fee_units': native, 'window_started_at': since, 'checked_at': now}


def check_fee(cursor, fee_limit, *, reservation_id=None):
    """Caller holds operator:protocol-budget. A reservation being sent is excluded from the pending ceilings it rejoins."""
    units(fee_limit)
    cap = current(cursor)
    if cap is None:
        raise Conflict('no spending cap is recorded; the pool commits no submission fees without one')
    if fee_limit > int(cap['tig_fee_attempt_units']):
        raise Conflict('submission fee ceiling exceeds the per-attempt spending cap')
    used = committed(cursor, cap, exclude=reservation_id)['tig_fee_units']
    if used + fee_limit > int(cap['tig_fee_window_units']):
        raise Conflict('protocol fee spending cap for the current window is reached')


def check_gas(cursor, fee_limit):
    """Serialises custody sends on their own fence; withdrawals do not hold the protocol budget lock."""
    units(fee_limit, positive=True)
    cap = current(cursor)
    if cap is None:
        raise Conflict('no spending cap is recorded; the pool reserves no custody gas without one')
    if fee_limit > int(cap['native_fee_attempt_units']):
        raise Conflict('gas ceiling exceeds the per-send spending cap')
    lock(cursor, 'operator:spending-cap')
    used = committed(cursor, cap)['native_fee_units']
    if used + fee_limit > int(cap['native_fee_window_units']):
        raise Conflict('custody gas spending cap for the current window is reached')


def blocked(cursor):
    cap = current(cursor)
    if cap is None:
        return UNSET
    if committed(cursor, cap)['tig_fee_units'] >= int(cap['tig_fee_window_units']):
        return EXHAUSTED
    return None


def status(database, *, cursor=None, now=None):
    with (database.transaction() if cursor is None else nullcontext(cursor)) as query:
        cap = current(query)
        if cap is None:
            return {'configured': False, 'cap': None, 'window': None, 'block_reason': UNSET}
        used = committed(query, cap, now=now)
        remaining = {'tig_fee_units': max(int(cap['tig_fee_window_units']) - used['tig_fee_units'], 0),
                     'native_fee_units': max(int(cap['native_fee_window_units']) - used['native_fee_units'], 0)}
        return {'configured': True, 'cap': cap, 'window': {**used, 'remaining': remaining},
                'block_reason': EXHAUSTED if remaining['tig_fee_units'] == 0 else None}
