"""An operator pause cannot enable capabilities disabled by deployment settings."""

from contextlib import nullcontext

from .database import lock
from .money import Conflict, FundsError


def paused(database, *, cursor=None):
    with (database.transaction() if cursor is None else nullcontext(cursor)) as query:
        query.execute("SELECT value FROM runtime_controls WHERE name='new_work_paused'")
        row=query.fetchone()
        return bool(row and row['value'])


def blocked(database,*,cursor=None):
    from .chain_observer import status
    if paused(database,cursor=cursor):return 'operator-pause'
    custody=status(database,cursor=cursor)
    if custody['initialized'] and not custody['ready']:return 'custody-reconciliation'
    return None


def set_pause(database,value,*,actor,reason,event_key):
    if type(value) is not bool or not actor or not reason or not event_key:
        raise FundsError('pause changes require an operator, reason and event key')
    with database.transaction() as cursor:
        # Reservations and first precommit sends take this same budget fence.
        lock(cursor,'operator:protocol-budget')
        cursor.execute('SELECT * FROM runtime_control_events WHERE event_key=%s',(event_key,))
        previous=cursor.fetchone()
        if previous:
            if (previous['after_value'],previous['actor'],previous['reason'])!=(value,actor,reason):
                raise Conflict('control event key was reused')
            return dict(previous)
        before=paused(database,cursor=cursor)
        cursor.execute("""INSERT INTO runtime_controls(name,value) VALUES ('new_work_paused',%s)
            ON CONFLICT (name) DO UPDATE SET value=excluded.value,updated_at=clock_timestamp()""",(value,))
        cursor.execute("""INSERT INTO runtime_control_events(event_key,name,before_value,after_value,actor,reason)
            VALUES (%s,'new_work_paused',%s,%s,%s,%s) RETURNING *""",(event_key,before,value,actor,reason))
        return dict(cursor.fetchone())
