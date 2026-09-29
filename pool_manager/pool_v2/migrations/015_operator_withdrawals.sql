-- Operator payouts use the same custody nonce, evidence and payment lifecycle
-- as member withdrawals, but can debit only the operator's available TIG.
ALTER TABLE withdrawals ALTER COLUMN member_id DROP NOT NULL;
ALTER TABLE withdrawals ADD COLUMN kind text NOT NULL DEFAULT 'member';
ALTER TABLE withdrawals ADD COLUMN operator_reserve numeric;
ALTER TABLE withdrawals ADD CONSTRAINT withdrawal_owner CHECK (
    (kind='member' AND member_id IS NOT NULL AND operator_reserve IS NULL) OR
    (kind='operator' AND member_id IS NULL AND operator_reserve IS NOT NULL
        AND operator_reserve>=0 AND operator_reserve=trunc(operator_reserve))
);
CREATE UNIQUE INDEX operator_withdrawal_request ON withdrawals(request_key) WHERE kind='operator';
CREATE UNIQUE INDEX one_pending_operator_withdrawal ON withdrawals(kind)
    WHERE kind='operator' AND state IN ('requested','approved','uncertain');

CREATE OR REPLACE FUNCTION protect_withdrawal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'withdrawal cannot be deleted'; END IF;
    IF (NEW.id,NEW.member_id,NEW.request_key,NEW.amount,NEW.recipient,NEW.created_at,NEW.kind,NEW.operator_reserve)
       IS DISTINCT FROM
       (OLD.id,OLD.member_id,OLD.request_key,OLD.amount,OLD.recipient,OLD.created_at,OLD.kind,OLD.operator_reserve)
       OR (OLD.paid_event IS NOT NULL AND NEW.paid_event IS DISTINCT FROM OLD.paid_event)
       OR (OLD.state IN ('paid','rejected','cancelled') AND NEW.state <> OLD.state)
    THEN RAISE EXCEPTION 'immutable withdrawal request or final outcome'; END IF;
    RETURN NEW;
END $$;
