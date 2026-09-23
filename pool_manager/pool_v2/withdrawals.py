"""Member withdrawal reservations. Transfer review/sending is a separate process."""

from datetime import datetime, timedelta, timezone
import uuid

from psycopg2.extras import Json

from . import custody, deposits, ledger
from .chain import CustodyPreflight, ConfirmedTransfer, ConfirmedTransaction, FEE_MODELS, hex_bytes
from .database import lock
from .members import available, member_lock
from .money import Conflict, FundsError, units


def pending(identity):
    return f"withdrawal:{identity}"


def request(database, member_id, request_key, amount):
    units(amount, positive=True)
    if not request_key or len(request_key) > 128:
        raise FundsError("withdrawal requires a bounded idempotency key")
    with database.transaction() as cursor:
        member = member_lock(cursor, member_id)
        cursor.execute("SELECT * FROM withdrawals WHERE member_id=%s AND request_key=%s", (member_id, request_key))
        existing = cursor.fetchone()
        if existing:
            if existing["amount"] != amount:
                raise Conflict("withdrawal key was reused with a different amount")
            return dict(existing)
        cursor.execute("SELECT clock_timestamp() AS now")
        now = cursor.fetchone()["now"]
        if member["last_paid_at"] and now < member["last_paid_at"] + timedelta(days=7):
            raise Conflict("seven days must pass after the previous successful withdrawal")
        cursor.execute("SELECT 1 FROM withdrawals WHERE member_id=%s AND state IN ('requested','approved','uncertain')",
                       (member_id,))
        if cursor.fetchone():
            raise Conflict("member already has a pending withdrawal")
        identity = uuid.uuid4()
        ledger.account(cursor, pending(identity), "withdrawal")
        ledger.post(cursor, f"withdrawal:{identity}:reserve", "withdrawal_reservation",
                    [(available(member_id), -amount), (pending(identity), amount)])
        cursor.execute("""INSERT INTO withdrawals(id,member_id,request_key,amount,recipient)
            VALUES (%s,%s,%s,%s,%s) RETURNING *""", (identity, member_id, request_key, amount, member["withdrawal_wallet"]))
        return dict(cursor.fetchone())


def _locked(cursor, identity):
    cursor.execute('SELECT member_id FROM withdrawals WHERE id=%s', (identity,))
    initial = cursor.fetchone()
    if not initial: raise FundsError('unknown withdrawal request')
    member_lock(cursor, initial['member_id'])
    cursor.execute('SELECT * FROM withdrawals WHERE id=%s FOR UPDATE', (identity,))
    return dict(cursor.fetchone())


def _event(cursor, identity, kind, actor, details, key):
    if not actor or not key: raise FundsError('withdrawal action requires actor and event key')
    cursor.execute('SELECT * FROM withdrawal_events WHERE event_key=%s', (key,))
    previous = cursor.fetchone()
    if previous:
        if (str(previous['withdrawal_id']), previous['kind'], previous['actor'], previous['details']) != (str(identity), kind, actor, details):
            raise Conflict('withdrawal event key was reused with different inputs')
        return
    cursor.execute('INSERT INTO withdrawal_events(event_key,withdrawal_id,kind,actor,details) VALUES (%s,%s,%s,%s,%s)',
                   (key, identity, kind, actor, Json(details)))


def approve(database, identity, network, *, fee_model, actor, evidence):
    if fee_model not in FEE_MODELS or not actor or not evidence or not network.require_finalized:
        raise FundsError('review requires a verified fee model, operator and evidence')
    with database.transaction() as cursor:
        custody.bind(cursor, network)
        row = _locked(cursor, identity)
        cursor.execute('SELECT * FROM withdrawal_reviews WHERE withdrawal_id=%s', (identity,))
        previous = cursor.fetchone()
        route = (network.chain_id, network.token, network.custody, fee_model, network.confirmations)
        if previous:
            if (previous['chain_id'], previous['token'], previous['sender'], previous['fee_model'], previous['confirmations']) != route:
                raise Conflict('withdrawal payment route is already frozen')
            return dict(previous)
        if row['state'] != 'requested': raise Conflict('only a pending request can be approved')
        if row['recipient'] == network.custody: raise Conflict('a withdrawal cannot send back to the pool custody wallet')
        cursor.execute('''INSERT INTO withdrawal_reviews(withdrawal_id,chain_id,token,sender,fee_model,confirmations,actor,evidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''', (identity, *route, actor, Json(evidence)))
        result = dict(cursor.fetchone())
        cursor.execute("UPDATE withdrawals SET state='approved' WHERE id=%s", (identity,))
        return result


def release(database, identity, *, actor, reason, event_key, member_id=None):
    """Reject as operator, or cancel as owner, only before any unresolved send."""
    if not reason: raise FundsError('withdrawal release requires a reason')
    state = 'cancelled' if member_id else 'rejected'
    with database.transaction() as cursor:
        row = _locked(cursor, identity)
        if member_id and str(row['member_id']) != str(member_id): raise FundsError('withdrawal belongs to another member')
        if row['state'] not in ('requested', 'approved', state):
            raise Conflict('potentially sent or paid withdrawals cannot be released')
        details = {'reason': reason}
        scoped_key = f'member:{member_id}:{event_key}' if member_id else 'operator:'+event_key
        _event(cursor, identity, state, actor, details, scoped_key)
        if row['state'] == state: return row
        ledger.post(cursor, f'withdrawal:{identity}:release', 'withdrawal_release',
            [(pending(identity), -int(row['amount'])), (available(row['member_id']), int(row['amount']))],
            {'actor': actor, 'reason': reason, 'state': state})
        cursor.execute('UPDATE withdrawals SET state=%s WHERE id=%s RETURNING *', (state, identity))
        return dict(cursor.fetchone())


def gas_hold(identity):
    return 'withdrawal-gas:'+str(identity)


def begin(database, identity, request_key, preflight, *, fee_limit, actor):
    """Commit the nonce, member hold and operator fee budget before manual send."""
    units(fee_limit, positive=True)
    if not isinstance(preflight, CustodyPreflight) or not request_key or len(request_key)>128 or not actor:
        raise FundsError('sending requires a verified custody preflight, bounded key and actor')
    with database.transaction() as cursor:
        custody.bind(cursor, preflight.network)
        row = _locked(cursor, identity)
        cursor.execute('SELECT * FROM withdrawal_attempts WHERE withdrawal_id=%s AND request_key=%s', (identity, request_key))
        previous = cursor.fetchone()
        if previous:
            if int(previous['fee_limit']) != fee_limit: raise Conflict('send attempt key was reused with a different fee limit')
            return dict(previous)
        age = (datetime.now(timezone.utc) - preflight.checked_at).total_seconds()
        if not -5 <= age <= 20: raise Conflict('custody preflight is stale; refresh before starting an attempt')
        if row['state'] != 'approved': raise Conflict('withdrawal is not approved for a new send attempt')
        cursor.execute('SELECT * FROM withdrawal_reviews WHERE withdrawal_id=%s', (identity,))
        review = cursor.fetchone()
        if not review or (review['chain_id'], review['token'], review['sender']) != (
            preflight.network.chain_id, preflight.network.token, preflight.network.custody):
            raise Conflict('withdrawal network or custody route differs from its approval')
        if not preflight.network.require_finalized or preflight.network.confirmations < review['confirmations']:
            raise Conflict('withdrawal confirmation policy cannot be weakened')
        if (ledger.backing(cursor, 'TIG'), ledger.backing(cursor, 'NATIVE')) != (preflight.token_balance, preflight.native_balance):
            raise Conflict('confirmed wallet balances do not reconcile to all recorded custody funds')
        from .chain_observer import status
        observed=status(database,cursor=cursor)
        if observed['initialized'] and not observed['ready']:
            raise Conflict('custody observer requires reconciliation before another payment attempt')
        attempt_id = uuid.uuid4()
        custody.reserve_nonce(cursor,attempt_id,preflight.network,preflight.nonce,'withdrawal')
        account = ledger.account(cursor, gas_hold(attempt_id), 'operator_commitment', asset='NATIVE')
        ledger.post(cursor, f'withdrawal-attempt:{attempt_id}:gas', 'operator_withdrawal_fee_reservation',
            [('operator:custody:NATIVE', -fee_limit), (account, fee_limit)], {'withdrawal_id': str(identity), 'actor': actor})
        proof = {'block_number': preflight.block_number, 'checked_at': preflight.checked_at.isoformat(),
            'token_balance': preflight.token_balance, 'native_balance': preflight.native_balance, 'evidence': preflight.evidence}
        cursor.execute('''INSERT INTO withdrawal_attempts(id,withdrawal_id,request_key,chain_id,sender,nonce,fee_limit,actor,preflight)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (attempt_id, identity, request_key, review['chain_id'], review['sender'], preflight.nonce, fee_limit, actor, Json(proof)))
        result = dict(cursor.fetchone())
        cursor.execute("UPDATE withdrawals SET state='uncertain' WHERE id=%s", (identity,))
        return result


def claim_transaction(database, attempt_id, tx_hash, *, actor):
    """Remember a hash even if RPC verification is unavailable. It is not payment."""
    tx_hash = hex_bytes(tx_hash, 32)
    if not actor: raise FundsError('transaction claim requires operator identity')
    with database.transaction() as cursor:
        lock(cursor, 'withdrawal-claim:'+tx_hash)
        cursor.execute('SELECT chain_id FROM withdrawal_attempts WHERE id=%s', (attempt_id,))
        attempt = cursor.fetchone()
        if not attempt: raise FundsError('unknown withdrawal attempt')
        cursor.execute('SELECT attempt_id FROM withdrawal_transaction_claims WHERE attempt_id=%s AND chain_id=%s AND tx_hash=%s', (attempt_id, attempt['chain_id'], tx_hash))
        existing = cursor.fetchone()
        if existing:
            return
        cursor.execute('INSERT INTO withdrawal_transaction_claims(chain_id,tx_hash,attempt_id,actor) VALUES (%s,%s,%s,%s)',
                       (attempt['chain_id'], tx_hash, attempt_id, actor))


def reconcile(database, attempt_id, transaction, transfer=None):
    """Trusted read-only chain adapter: pay, or prove the nonce cannot pay later."""
    if not isinstance(transaction, ConfirmedTransaction): raise FundsError('confirmed transaction evidence is required')
    if transfer is not None and not isinstance(transfer, ConfirmedTransfer): raise FundsError('verified token event is required')
    with database.transaction() as cursor:
        # Preserve valid chain evidence even if operator funding needs topping up.
        cursor.execute('SELECT * FROM withdrawal_attempts WHERE id=%s', (attempt_id,))
        initial = cursor.fetchone()
        if not initial: raise FundsError('unknown withdrawal attempt')
        if (transaction.network.chain_id, transaction.sender, transaction.nonce) != (
                initial['chain_id'], initial['sender'], int(initial['nonce'])):
            raise Conflict('transaction does not match the frozen chain, sender or nonce')
        cursor.execute('SELECT * FROM withdrawal_reviews WHERE withdrawal_id=%s', (initial['withdrawal_id'],))
        initial_review=cursor.fetchone()
        if transaction.fee_model != initial_review['fee_model']:
            raise Conflict('transaction uses a different fee rule from its frozen approval')
        if (not transaction.network.require_finalized or transaction.network.confirmations < initial_review['confirmations']
                or (transfer is not None and (not transfer.network.require_finalized or transfer.network.confirmations < initial_review['confirmations']))):
            raise Conflict('withdrawal confirmation policy cannot be weakened')
        if transaction.block_timestamp + timedelta(seconds=5) < initial['sent_at']:
            raise Conflict('transaction predates the recorded send attempt')
        custody.save_transaction(cursor, transaction)
    with database.transaction() as cursor:
        custody.bind(cursor, transaction.network)
        cursor.execute('SELECT * FROM withdrawal_attempts WHERE id=%s', (attempt_id,))
        attempt = cursor.fetchone()
        if not attempt: raise FundsError('unknown withdrawal attempt')
        row = _locked(cursor, attempt['withdrawal_id'])
        cursor.execute('SELECT * FROM withdrawal_reviews WHERE withdrawal_id=%s', (row['id'],))
        review = cursor.fetchone()
        if (transaction.network.chain_id, transaction.sender, transaction.nonce, transaction.fee_model) != (
            attempt['chain_id'], attempt['sender'], int(attempt['nonce']), review['fee_model']):
            raise Conflict('transaction does not match the frozen chain, sender, nonce or fee rule')
        if (not transaction.network.require_finalized or transaction.network.confirmations < review['confirmations']
                or (transfer is not None and (not transfer.network.require_finalized or transfer.network.confirmations < review['confirmations']))):
            raise Conflict('withdrawal confirmation policy cannot be weakened')
        if transaction.block_number <= attempt['preflight']['block_number']:
            raise Conflict('transaction predates the recorded send attempt')
        cursor.execute('SELECT * FROM withdrawal_attempt_outcomes WHERE attempt_id=%s', (attempt_id,))
        previous = cursor.fetchone()
        if previous:
            if previous['tx_hash'] != transaction.tx_hash: raise Conflict('attempt already has a different final transaction')
            if previous['outcome']=='paid' and (not isinstance(transfer, ConfirmedTransfer) or row['paid_event'] != transfer.event_id):
                raise Conflict('payment replay must identify the same exact transfer event')
            if transfer is not None: deposits.save_transfer(cursor, transfer)
            return dict(previous)
        if row['state'] != 'uncertain': raise Conflict('withdrawal has no unresolved send to reconcile')
        if not transaction.successful:
            if transfer is not None: raise Conflict('failed transaction cannot contain a successful withdrawal')
            outcome = 'failed'
        elif transfer is not None:
            if not isinstance(transfer, ConfirmedTransfer) or (transfer.network.chain_id, transfer.network.token,
                transfer.sender, transfer.recipient, transfer.amount, transfer.tx_hash, transfer.block_hash) != (
                review['chain_id'], review['token'], review['sender'], row['recipient'], int(row['amount']), transaction.tx_hash, transaction.block_hash):
                raise Conflict('verified transfer does not match the full frozen withdrawal')
            deposits.save_transfer(cursor, transfer)
            cursor.execute('SELECT id FROM withdrawals WHERE paid_event=%s', (transfer.event_id,))
            if cursor.fetchone(): raise Conflict('token event already paid another withdrawal')
            outcome = 'paid'
        else:
            tx = transaction.evidence['transaction']
            if transaction.recipient != transaction.sender or transaction.value != 0 or tx.get('input') not in ('0x', '') or transaction.evidence['receipt']['logs']:
                raise Conflict('successful unmatched transaction needs explicit wallet reconciliation')
            outcome = 'cancelled'  # A finalized empty self-send consumes the reserved nonce.
        fee_limit = int(attempt['fee_limit'])
        movements = [(gas_hold(attempt_id), -fee_limit), ('operator:custody:NATIVE', fee_limit-transaction.fee),
                     ('external:custody:NATIVE', transaction.fee)]
        if outcome == 'paid':
            movements += [(pending(row['id']), -int(row['amount'])), ('external:custody:TIG', int(row['amount']))]
        journal_id = ledger.post(cursor, f'withdrawal-attempt:{attempt_id}:reconcile', 'withdrawal_'+outcome, movements,
            {'withdrawal_id': str(row['id']), 'attempt_id': str(attempt_id), 'chain_id': transaction.network.chain_id,
             'tx_hash': transaction.tx_hash, 'fee': transaction.fee, 'fee_model': transaction.fee_model,
             'transfer_event': transfer.event_id if transfer else None})
        custody.record_payment(cursor,attempt_id,transaction,transfer.event_id if transfer else None,journal_id)
        cursor.execute('''INSERT INTO withdrawal_attempt_outcomes(attempt_id,chain_id,tx_hash,outcome,fee,journal_id)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
            (attempt_id, transaction.network.chain_id, transaction.tx_hash, outcome, transaction.fee, journal_id))
        result = dict(cursor.fetchone())
        if outcome == 'paid':
            cursor.execute("UPDATE withdrawals SET state='paid',paid_event=%s WHERE id=%s", (transfer.event_id, row['id']))
            cursor.execute('UPDATE members SET last_paid_at=%s WHERE id=%s', (transaction.block_timestamp, row['member_id']))
        else:
            cursor.execute("UPDATE withdrawals SET state='approved' WHERE id=%s", (row['id'],))
        return result


def reconcile_sponsored(database, attempt_id, recovery, *, actor, reason):
    """Account once for a verified initial EIP-7702 payment without a resend."""
    from .sponsored_withdrawals import SponsoredWithdrawal
    if not isinstance(recovery, SponsoredWithdrawal) or not actor or not reason:
        raise FundsError('verified sponsored recovery and operator review required')
    tx, transfer = recovery.transaction, recovery.transfer
    if not tx.successful or tx.sender == tx.network.custody:
        raise Conflict('recovery must be a successful externally sponsored transaction')
    with database.transaction() as cursor:
        custody.bind(cursor, tx.network)
        cursor.execute('SELECT * FROM withdrawal_attempts WHERE id=%s', (attempt_id,))
        attempt = cursor.fetchone()
        if not attempt:
            raise FundsError('unknown withdrawal attempt')
        row = _locked(cursor, attempt['withdrawal_id'])
        cursor.execute('SELECT * FROM withdrawal_reviews WHERE withdrawal_id=%s', (row['id'],))
        review = cursor.fetchone()
        if (attempt['chain_id'], attempt['sender'], int(attempt['nonce']), review['token'], review['fee_model']) != (
                tx.network.chain_id, tx.network.custody, recovery.custody_nonce, tx.network.token, tx.fee_model):
            raise Conflict('sponsored recovery differs from the frozen custody route')
        if (not tx.network.require_finalized or tx.network.confirmations < review['confirmations']
                or transfer.network != tx.network):
            raise Conflict('sponsored recovery cannot weaken finality or change network')
        if (tx.block_timestamp + timedelta(seconds=5) < attempt['sent_at']
                or tx.block_number <= attempt['preflight']['block_number']):
            raise Conflict('sponsored payment predates the prepared attempt')
        if (transfer.sender, transfer.recipient, transfer.amount, transfer.tx_hash, transfer.block_hash) != (
                attempt['sender'], row['recipient'], int(row['amount']), tx.tx_hash, tx.block_hash):
            raise Conflict('sponsored token event differs from the full frozen withdrawal')
        cursor.execute('SELECT * FROM withdrawal_attempt_outcomes WHERE attempt_id=%s', (attempt_id,))
        previous = cursor.fetchone()
        if previous:
            cursor.execute('SELECT delegate FROM custody_authorization_payments WHERE send_id=%s', (attempt_id,))
            authorization = cursor.fetchone()
            if (previous['tx_hash'] != tx.tx_hash or previous['outcome'] != 'paid'
                    or row['paid_event'] != transfer.event_id or int(previous['fee']) != 0
                    or not authorization or authorization['delegate'] != recovery.delegate):
                raise Conflict('attempt already has different financial attribution')
            custody.save_transaction(cursor, tx); deposits.save_transfer(cursor, transfer)
            return dict(previous)
        if row['state'] != 'uncertain':
            raise Conflict('withdrawal has no unresolved payment to recover')
        custody.save_transaction(cursor, tx)
        deposits.save_transfer(cursor, transfer)
        cursor.execute('SELECT id FROM withdrawals WHERE paid_event=%s', (transfer.event_id,))
        if cursor.fetchone():
            raise Conflict('token event already paid another withdrawal')
        fee_limit = int(attempt['fee_limit'])
        journal_id = ledger.post(cursor, f'withdrawal-attempt:{attempt_id}:reconcile', 'withdrawal_paid',
            [(gas_hold(attempt_id), -fee_limit), ('operator:custody:NATIVE', fee_limit),
             (pending(row['id']), -int(row['amount'])), ('external:custody:TIG', int(row['amount']))],
            {'withdrawal_id': str(row['id']), 'attempt_id': str(attempt_id), 'chain_id': tx.network.chain_id,
             'tx_hash': tx.tx_hash, 'fee': 0, 'relayer_fee': tx.fee, 'fee_model': tx.fee_model,
             'transfer_event': transfer.event_id, 'actor': actor, 'reason': reason,
             'route': 'initial-eip7702-sponsored'})
        custody.record_payment(cursor, attempt_id, tx, transfer.event_id, journal_id,
                               authorization=recovery, actor=actor, reason=reason)
        cursor.execute('''INSERT INTO withdrawal_attempt_outcomes
            (attempt_id,chain_id,tx_hash,outcome,fee,journal_id)
            VALUES (%s,%s,%s,'paid',0,%s) RETURNING *''',
            (attempt_id, tx.network.chain_id, tx.tx_hash, journal_id))
        outcome = dict(cursor.fetchone())
        cursor.execute("UPDATE withdrawals SET state='paid',paid_event=%s WHERE id=%s", (transfer.event_id, row['id']))
        cursor.execute('UPDATE members SET last_paid_at=%s WHERE id=%s', (tx.block_timestamp, row['member_id']))
        return outcome


def instructions(database, attempt_id):
    """Frozen manual wallet instructions. Reading them never authorizes a resend."""
    with database.transaction() as cursor:
        cursor.execute('''SELECT a.*,w.recipient,w.amount,w.state,r.token,r.fee_model,
            o.outcome,o.tx_hash AS final_tx_hash FROM withdrawal_attempts a
            JOIN withdrawals w ON w.id=a.withdrawal_id JOIN withdrawal_reviews r ON r.withdrawal_id=w.id
            LEFT JOIN withdrawal_attempt_outcomes o ON o.attempt_id=a.id WHERE a.id=%s''', (attempt_id,))
        row = cursor.fetchone()
        if not row: raise FundsError('unknown withdrawal attempt')
        value = dict(row)
        cursor.execute('SELECT tx_hash FROM withdrawal_transaction_claims WHERE attempt_id=%s ORDER BY created_at', (attempt_id,))
        value['claimed_tx_hashes'] = [claim['tx_hash'] for claim in cursor.fetchall()]
        value['transaction'] = {'from': row['sender'], 'to': row['token'], 'chainId': hex(row['chain_id']),
            'nonce': hex(int(row['nonce'])), 'value': '0x0',
            'data': '0xa9059cbb'+'0'*24+row['recipient'][2:]+f"{int(row['amount']):064x}"}
        return value
