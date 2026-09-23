"""Read-only evidence for TIG's TokenLocker reward and withdrawal lifecycle.

Claimable and locked rewards are not custody funds. Events contain no TIG round
number: a distribution comparison supports review, not automatic attribution.
"""
from datetime import datetime, timezone
import hashlib
import re

from eth_utils import keccak

from .chain import hex_bytes, quantity
from .ledger import fingerprint
from .members import address
from .money import FundsError, units


EVENTS = {
    'TokensRewarded(address,uint256,uint256)': ('rewarded', ('amount', 'claimable')),
    'TokensClaimed(address,uint256,uint256)': ('claimed', ('amount', 'locked')),
    'TokensLocked(address,uint256,uint256)': ('locked', ('amount', 'locked')),
    'TokensUnlocked(address,uint256,uint256,uint256)': ('unlocked', ('amount', 'locked', 'withdrawable_time')),
    'TokensRelocked(address,uint256,uint256)': ('relocked', ('amount', 'locked')),
    'TokensWithdrawn(address,uint256)': ('withdrawn', ('amount',)),
}
TOPICS = {'0x'+keccak(text=name).hex(): value for name,value in EVENTS.items()}


def selector(signature):
    return '0x'+keccak(text=signature)[:4].hex()


def call(chain, locker, signature, arguments, block):
    data=selector(signature)+''.join(f'{value:064x}' for value in arguments)
    return chain.rpc('eth_call',[{'to':locker,'data':data},block])


def verify_contract(chain, locker, code_sha256, block):
    locker=address(locker)
    if not isinstance(code_sha256,str) or not re.fullmatch('[0-9a-f]{64}',code_sha256):
        raise FundsError('reward contract requires a pinned deployed-code checksum')
    code=chain.rpc('eth_getCode',[locker,block])
    if not isinstance(code,str) or not re.fullmatch('0x(?:[0-9a-fA-F]{2})+',code):
        raise FundsError('configured reward contract has no valid deployed code')
    if hashlib.sha256(bytes.fromhex(code[2:])).hexdigest()!=code_sha256:
        raise FundsError('reward contract bytecode differs from the reviewed implementation')
    token=hex_bytes(call(chain,locker,'token()',[],block),32)
    if token[2:26]!='0'*24 or '0x'+token[-40:]!=chain.network.token:
        raise FundsError('reward contract uses a different token')
    return locker


def snapshot(chain, locker, code_sha256):
    chain.verify_network()
    if not chain.network.require_finalized:
        raise FundsError('reward observation requires finalized chain evidence')
    head=chain.rpc('eth_getBlockByNumber',['finalized',False])
    if not head:raise FundsError('finalized reward observation is unavailable')
    height=quantity(head['number']); block=hex(height)
    locker=verify_contract(chain,locker,code_sha256,block)
    user=int(chain.network.custody,16)
    values={name:int(hex_bytes(call(chain,locker,name+'(address)',[user],block),32),16)
            for name in ('claimable','locked','getNumPendingWithdrawals')}
    pending_period=int(hex_bytes(call(chain,locker,'pendingPeriod()',[],block),32),16)
    if values['getNumPendingWithdrawals']>256:
        raise FundsError('too many pending reward withdrawals for one bounded observation')
    pending=[]
    for index in range(values['getNumPendingWithdrawals']):
        raw=hex_bytes(call(chain,locker,'pendingWithdrawals(address,uint256)',[user,index],block),64)
        amount,when=int(raw[2:66],16),int(raw[66:],16)
        pending.append({'index':index,'amount':str(amount),'withdrawable_timestamp':when,
                        'ready':when<=quantity(head['timestamp'])})
    return {'chain_id':chain.network.chain_id,'token':chain.network.token,
        'wallet':chain.network.custody,'locker':locker,'code_sha256':code_sha256,
        'block_number':height,'block_hash':hex_bytes(head['hash'],32),
        'block_timestamp':quantity(head['timestamp']),
        'checked_at':datetime.now(timezone.utc).isoformat(),
        'claimable':str(values['claimable']),'locked':str(values['locked']),
        'pending_withdrawals':pending,'pending_period_seconds':pending_period,
        'custody_receipt_required':True,'round_attribution_required':True}


def events(chain, locker, code_sha256, tx_hash):
    """Verify all lifecycle events in one finalized transaction, without posting money."""
    tx_hash=hex_bytes(tx_hash,32)
    evidence=chain._confirmed_receipt(tx_hash)
    receipt,head=evidence['receipt'],evidence['header']
    if quantity(receipt['status'])!=1:raise FundsError('reward transaction did not succeed')
    if not chain.network.require_finalized:
        raise FundsError('reward events require finalized chain evidence')
    locker=verify_contract(chain,locker,code_sha256,hex(quantity(receipt['blockNumber'])))
    result=[];seen=set()
    for log in receipt['logs']:
        if address(log['address'])!=locker:continue
        topics=log.get('topics',[])
        if not topics or topics[0].lower() not in TOPICS:continue
        if (log.get('removed') or hex_bytes(log['transactionHash'],32)!=tx_hash
                or hex_bytes(log['blockHash'],32)!=hex_bytes(receipt['blockHash'],32)
                or quantity(log['blockNumber'])!=quantity(receipt['blockNumber'])):
            raise FundsError('reward event is not canonically included in this receipt')
        if len(topics)!=2:raise FundsError('reward event has malformed indexed fields')
        encoded_user=hex_bytes(topics[1],32)
        if encoded_user[2:26]!='0'*24:raise FundsError('reward event has malformed beneficiary')
        kind,fields=TOPICS[topics[0].lower()]
        data=hex_bytes(log['data'],32*len(fields))[2:]
        values={field:int(data[index*64:(index+1)*64],16) for index,field in enumerate(fields)}
        units(values['amount'],positive=True)
        index=quantity(log['logIndex'])
        if index in seen:raise FundsError('duplicate reward event identity')
        seen.add(index)
        result.append({'event_id':f'{chain.network.chain_id}:{locker}:{tx_hash}:{index}',
            'kind':kind,'user':'0x'+encoded_user[-40:],'log_index':index,
            **{key:str(value) for key,value in values.items()}})
    return {'chain_id':chain.network.chain_id,'locker':locker,'token':chain.network.token,
        'code_sha256':code_sha256,'tx_hash':tx_hash,'block_number':quantity(receipt['blockNumber']),
        'block_hash':hex_bytes(receipt['blockHash'],32),'block_timestamp':quantity(head['timestamp']),
        'events':result,'evidence':evidence}


def compare_distribution(round_number, emissions, verified_events):
    """Exact all-player comparison; extra treasury entries are surfaced for review."""
    units(round_number,positive=True)
    try:
        expected={}
        for user,components in emissions['players'].items():
            user=address(user)
            if user in expected or not isinstance(components,dict):
                raise FundsError('duplicate or malformed earnings beneficiary')
            values=list(components.values())
            if any(not isinstance(value,str) or not re.fullmatch('[0-9]{1,78}',value) for value in values):
                raise FundsError('earnings must contain exact nonnegative integer token units')
            expected[user]=sum(map(int,values))
        actual={}
        for event in verified_events['events']:
            if event['kind']=='rewarded':
                user=address(event['user']);amount=int(event['amount']);units(amount,positive=True)
                actual[user]=actual.get(user,0)+amount
    except (KeyError,TypeError,ValueError,AttributeError) as error:
        raise FundsError('incomplete reward distribution evidence') from error
    expected={user:amount for user,amount in expected.items() if amount}
    if not expected:raise FundsError('an empty earnings response cannot identify a distribution')
    mismatches={user:{'expected':str(amount),'rewarded':str(actual.get(user,0))}
                for user,amount in expected.items() if actual.get(user,0)!=amount}
    extras={user:str(amount) for user,amount in actual.items() if user not in expected}
    return {'round':round_number,'tx_hash':verified_events['tx_hash'],
        'emissions_sha256':fingerprint(emissions),'all_player_amounts_match':not mismatches,
        'expected_players':len(expected),'mismatches':mismatches,'unlisted_allocations':extras,
        'round_attribution_requires_review':True,'custody_receipt_proven':False}
