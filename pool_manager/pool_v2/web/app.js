const $ = id => document.getElementById(id);
const operatorPage = location.pathname === '/operator';
let token = '', capabilities = {}, memberOffset = 0, operatorOffset = 0, dialogAction = null;
const unit = 10n ** 18n;
const key = () => crypto.randomUUID();
function tig(value) {
  const amount = BigInt(value ?? '0'), sign = amount < 0n ? '-' : '', absolute = amount < 0n ? -amount : amount;
  const fraction = (absolute % unit).toString().padStart(18, '0').replace(/0+$/, '');
  return sign + (absolute / unit).toString() + (fraction ? '.' + fraction : '');
}
function units(value) {
  if (!/^(?:0|[1-9]\d*)(?:\.\d{1,18})?$/.test(value)) throw Error('Enter an amount with up to 18 decimal places.');
  const [whole, fraction = ''] = value.split('.');
  const amount = BigInt(whole) * unit + BigInt(fraction.padEnd(18, '0'));
  if (amount <= 0n) throw Error('Enter a positive amount.');
  return amount.toString();
}
function credit(numerator, denominator) {
  let n = BigInt(numerator), d = BigInt(denominator), remainder = n % d, digits = '';
  for (let i = 0; remainder && i < 18; i++) { remainder *= 10n; digits += (remainder / d).toString(); remainder %= d; }
  return remainder ? numerator + '/' + denominator : (n / d).toString() + (digits ? '.' + digits : '');
}
const short = value => value ? value.slice(0, 8) + '…' + value.slice(-6) : 'Awaiting identity';
const date = value => value ? new Date(value).toLocaleString() : '—';
const status = value => ({reserved:'Reserved',uncertain:'Awaiting confirmation',accepted:'In progress',active:'Active',verification_failed:'Verification failed',expired:'Expired',cancelled:'Cancelled',rejected:'Rejected',requested:'Awaiting review',approved:'Approved',paid:'Paid',awaiting_protocol:'Awaiting TIG credit',credited:'Credited',failed:'Failed'}[value] || value);
function node(tag, text, className) { const value = document.createElement(tag); if (text !== undefined) value.textContent = text; if (className) value.className = className; return value; }
function address(value) { const span = node('span', short(value), 'mono'); span.title = value || ''; return span; }
function button(label, action, className = 'secondary') { const value = node('button', label, className); value.type = 'button'; value.addEventListener('click', () => run(action, value)); return value; }
function notice(text, error = false) { $('message').hidden = !text; $('message').textContent = text; $('message').className = 'notice' + (error ? ' error' : ''); }
async function run(action, control) { if (control) control.disabled = true; try { await action(); } catch (error) { notice(error.message, true); } finally { if (control) control.disabled = false; } }
async function api(path, body, method) {
  const headers = {'X-InnoPool-Version':'2.0'};
  if (token) headers.Authorization = 'Bearer ' + token;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api/v2/' + path, {method:method || (body === undefined ? 'GET' : 'POST'),headers,body:body === undefined ? undefined : JSON.stringify(body),cache:'no-store',credentials:'omit'});
  const result = await response.json();
  if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'The request could not be processed. Check the entered values.');
  return result;
}
function table(id, rows, columns, empty = 'No records yet.') {
  const target = $(id); target.replaceChildren();
  for (const cells of rows) { const row = node('tr'); for (const item of cells) { const cell = node('td'); if (item instanceof Node) cell.append(item); else cell.textContent = String(item ?? '—'); row.append(cell); } target.append(row); }
  if (!rows.length) { const cell = node('td', empty, 'empty'); cell.colSpan = columns; const row = node('tr'); row.append(cell); target.append(row); }
}
async function loadCapabilities() {
  capabilities = await api('capabilities');
  $('work-status').textContent = capabilities.work_enabled ? 'Accepting work' : capabilities.new_work_paused ? 'New work paused' : capabilities.work_block_reason==='custody-reconciliation' ? 'Checking pool funds' : capabilities.work_block_reason==='protocol-fee-reconciliation' ? 'Checking submission funds' : 'Work not enabled';
  $('worker-install-options').hidden=!capabilities.release_digest;
  $('worker-install-status').textContent=capabilities.release_digest?'Download the installer paired with this pool. It asks privately for your execution token.':'The paired v2 installer will be available with the verified release.';
  $('work-status').classList.toggle('active', capabilities.work_enabled);
}
function field(label, id, value = '', options = {}) {
  const wrapper = node('div'), caption = node('label', label), input = node('input');
  caption.htmlFor = id; input.id = id; input.value = value; input.required = !options.optional;
  input.type = options.type || 'text'; input.autocomplete = 'off'; if (options.placeholder) input.placeholder = options.placeholder;
  wrapper.append(caption, input); $('action-fields').append(wrapper); return input;
}
function dialog(title, description, submitLabel, action) {
  $('action-title').textContent = title; $('action-description').textContent = description;
  $('action-fields').replaceChildren(); $('action-error').textContent = ''; $('action-submit').textContent = submitLabel;
  $('action-submit').disabled = false; dialogAction = action; $('action-dialog').showModal();
}
$('close-dialog').addEventListener('click', () => $('action-dialog').close());
$('action-form').addEventListener('submit', async event => {
  event.preventDefault(); $('action-submit').disabled = true; $('action-error').textContent = '';
  try { if (await dialogAction() !== false) $('action-dialog').close(); } catch (error) { $('action-error').textContent = error.message; }
  finally { $('action-submit').disabled = false; }
});
async function sign(message, wallet) {
  const encoded = '0x' + [...new TextEncoder().encode(message)].map(byte => byte.toString(16).padStart(2,'0')).join('');
  return window.ethereum.request({method:'personal_sign',params:[encoded,wallet]});
}
async function connect() {
  if (!window.ethereum) throw Error('Open this page with an Ethereum wallet extension to connect.');
  if (capabilities.origin.replace(/\/$/,'') !== location.origin) throw Error('The pool’s configured address does not match this page. Contact the operator.');
  const wallets = await window.ethereum.request({method:'eth_requestAccounts'});
  const chain = await window.ethereum.request({method:'eth_chainId'});
  if (BigInt(chain) !== BigInt(capabilities.chain_id)) throw Error('Select the pool’s network in your wallet (chain '+capabilities.chain_id+').');
  const challenge = await api('auth/challenges',{wallet:wallets[0]});
  const session = await api('auth/sessions',{challenge_id:challenge.id,signature:await sign(challenge.message,wallets[0])});
  token = session.token; await loadMember();
}
$('connect-wallet').addEventListener('click', () => run(connect,$('connect-wallet')));
async function signOut() {
  const previous = token; try { if (previous && !operatorPage) await api('auth/revoke',{token:previous}); } finally { token=''; $('worker-token').value=''; $('operator-token').value=''; location.reload(); }
}
$('sign-out').addEventListener('click', () => run(signOut)); $('operator-sign-out').addEventListener('click', () => run(signOut));
window.addEventListener('pagehide', () => {token=''; $('worker-token').value=''; $('operator-token').value='';});
window.addEventListener('pageshow', event => {if(event.persisted)location.reload();});
$('refresh-member').addEventListener('click',()=>run(async()=>{await loadCapabilities();await loadMember();},$('refresh-member')));
async function loadMember() {
  const data = await api('member/dashboard?limit=50&offset='+memberOffset), balance = data.balance;
  $('member-guest').hidden=true; $('member-content').hidden=false; $('connect-wallet').hidden=true; $('sign-out').hidden=false; $('refresh-member').hidden=false;
  $('connected-wallet').textContent=short(balance.wallet); $('connected-wallet').title=balance.wallet;
  $('available').textContent=tig(balance.available); $('collateral').textContent=tig(balance.collateral); $('pending').textContent=tig(balance.pending_withdrawals);
  $('multiplier').textContent=balance.multiplier+'×'; $('slots').textContent=balance.slots+' / 2 slots occupied';
  $('withdrawal-wallet').textContent=balance.withdrawal_wallet;
  $('withdraw-form').querySelector('button').disabled=!capabilities.funds_enabled;
  $('deposit-help').textContent=capabilities.funds_enabled && capabilities.custody ? 'Send TIG from your verified member wallet to the pool address below. Deposits from another source require operator review.' : 'Deposits and new withdrawals are not enabled for this deployment.';
  $('deposit-address').textContent=capabilities.funds_enabled ? capabilities.custody || '' : '';
  $('deposit-network').textContent=capabilities.funds_enabled && capabilities.custody ? 'Network: '+(capabilities.chain_id===8453?'Base (8453)':'Chain '+capabilities.chain_id)+' · TIG contract: '+capabilities.token : '';
  table('member-benchmarks',data.assignments.map(row=>[address(row.benchmark_id),row.resource,row.creation_round,status(row.state),tig(row.base_amount)+' × '+row.multiplier,tig(row.held)]),6,'Your benchmarks will appear here when your worker receives work.');
  table('member-rounds',data.rounds.map(row=>[row.round,credit(row.credit_numerator,row.credit_denominator),row.settled_at ? 'Settled' : 'Awaiting settlement',row.allocation===null ? '—' : tig(row.allocation)]),4,'No qualifying credit has been recorded for your account yet.');
  table('member-withdrawals',data.withdrawals.map(row=>[date(row.created_at),tig(row.amount),address(row.recipient),status(row.state),['requested','approved'].includes(row.state) ? button('Cancel',async()=>{await api('withdrawals/'+row.id+'/cancel',{reason:'Cancelled by member',event_key:key()});await loadMember();}) : '—']),5);
  $('member-page-number').textContent='Activity page '+(memberOffset/50+1); $('member-newer').disabled=memberOffset===0;
  $('member-older').disabled=data.assignments.length<50 && data.withdrawals.length<50;
  await loadTokens();
  if (location.pathname==='/join') $('worker-card').scrollIntoView({block:'center'});
}
$('withdraw-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{await api('withdrawals',{amount:units($('withdraw-amount').value.trim()),request_key:key()});$('withdraw-amount').value='';await loadMember();notice('Withdrawal requested. Your full amount is reserved for operator review.');},$('withdraw-form').querySelector('button'));});
async function loadTokens() {
  const data=await api('auth/execution-tokens');$('active-tokens').replaceChildren();
  for(const item of data.tokens){const row=node('div',undefined,'alert-item');row.append(node('span','Created '+date(item.created_at)+' · expires '+date(item.expires_at)),button('Revoke',async()=>{await api('auth/execution-tokens/'+item.id+'/revoke',{});$('worker-token').value='';$('worker-token-box').hidden=true;await loadTokens();notice('Execution token revoked. Configure a replacement token to keep that worker connected.');}));$('active-tokens').append(row);}
}
$('issue-worker-token').addEventListener('click',()=>run(async()=>{const result=await api('auth/execution-tokens',{});$('worker-token').value=result.token;$('worker-token-box').hidden=false;await loadTokens();},$('issue-worker-token')));
$('copy-worker-token').addEventListener('click',()=>run(async()=>{await navigator.clipboard.writeText($('worker-token').value);notice('Execution token copied.');}));
$('worker-install-form').addEventListener('submit',event=>{
  event.preventDefault();run(async()=>{
    const compute=$('worker-compute').value,resource=compute==='aws_g4dn'?'GPU':'CPU',workers=$('worker-capacity').value;
    const guide=await api('worker-installation?'+new URLSearchParams({resource,compute_type:compute,workers}));
    $('worker-install-command').textContent=guide.command+'\n'+guide.start_command;
    $('worker-installer-download').href=guide.installer_url;
    $('worker-installer-checksum').textContent='Installer SHA-256: '+guide.installer_sha256;
    $('worker-install-guide').hidden=false;
  },$('worker-install-form').querySelector('button'));
});
$('change-wallet').addEventListener('click',()=>{
  dialog('Change withdrawal wallet','Sign with the new wallet to verify it. Current withdrawal requests keep their existing destination.','Verify wallet',async()=>{
    if (!window.ethereum) throw Error('A wallet extension is required.');
    const wallet=$('new-wallet').value.trim(), challenge=await api('member/withdrawal-wallet/challenges',{wallet});
    const signature=await sign(challenge.message,wallet);
    await api('member/withdrawal-wallet',{challenge_id:challenge.id,signature});await loadMember();notice('Withdrawal wallet updated for future requests.');
  });field('New withdrawal address','new-wallet','',{placeholder:'0x…'});
});
for (const [id,change] of [['member-newer',-50],['member-older',50]]) $(id).addEventListener('click',()=>run(async()=>{memberOffset=Math.max(0,memberOffset+change);await loadMember();}));
$('operator-login').addEventListener('submit',event=>{event.preventDefault();run(async()=>{token=$('operator-token').value;try{await loadOperator();$('operator-token').value='';}catch(error){token='';throw error;}});});
function editMultiplier(member) {
  dialog('Set collateral multiplier',member.wallet+' · applies to new benchmarks only. Existing holds are unchanged.','Save multiplier',async()=>{
    await api('operator/members/'+member.id+'/multiplier',{multiplier:$('new-multiplier').value.trim(),reason:$('change-reason').value.trim(),event_key:key()});await loadOperator();notice('New multiplier saved. Existing benchmark holds keep their original amount.');
  });field('Multiplier · 0 to 1','new-multiplier',member.multiplier);field('Reason','change-reason');
}
function reviewWithdrawal(row, approve) {
  dialog(approve?'Approve withdrawal':'Reject withdrawal',tig(row.amount)+' TIG to '+row.recipient,approve?'Approve':'Reject',async()=>{
    const body={reason:$('review-reason').value.trim()};if(!approve)body.event_key=key();
    await api('operator/withdrawals/'+row.id+(approve?'/approve':'/reject'),body);await loadOperator();
  });field('Review notes','review-reason');
}
function prepareWithdrawal(row) {
  dialog('Prepare manual payment',tig(row.amount)+' TIG to '+row.recipient+'. The next step reserves a payment attempt before you use the custody wallet.','Prepare payment',async()=>{
    const attempt=await api('operator/withdrawals/'+row.id+'/begin',{request_key:key(),fee_limit:units($('gas-budget').value.trim())});
    $('action-dialog').close();await loadOperator();showPayment(attempt);return false;
  });field('Maximum reserved operator fee · native token','gas-budget','0.0001');
}
function showPayment(attempt,topup=false) {
  dialog(topup?'Manual fee top-up details':'Manual payment details','This attempt is recorded as potentially sent. Check your custody wallet before sending. Use its recorded nonce; check the transfer here to finish reconciliation.','Check transfer',async()=>{
    const body={},hash=$('payment-hash').value.trim(),index=$('payment-index').value.trim();
    if(hash)body.tx_hash=hash;
    if(index){const parsed=Number(index);if(!Number.isSafeInteger(parsed)||parsed<0)throw Error('Enter a valid transfer event index.');body.log_index=parsed;}
    const result=await api('operator/'+(topup?'topups/':'withdrawal-attempts/')+attempt.id+'/reconcile',body);
    if(result.status==='awaiting_final_transaction'){ $('action-error').textContent='The transaction is not final yet. The amount remains reserved.';return false; }
    await loadOperator();notice(topup ? (result.state==='awaiting_protocol'?'Transfer verified. Submission credit is waiting for TIG confirmation.':'Top-up transaction reconciled: '+status(result.state)+'.') : result.outcome==='paid'?'Payment verified. The member received the full requested amount.':'Transaction reconciled. The request can be reviewed for another attempt.');
  });
  for (const [label,value] of [['Amount',tig(attempt.amount)+' TIG'],['From',attempt.sender],['Recipient',attempt.recipient],['Token contract',attempt.token],['Chain',String(attempt.chain_id)],['Nonce',String(attempt.nonce)]]) {const row=node('div',undefined,'payment-fact');row.append(node('span',label),node('span',value,'mono'));$('action-fields').append(row);}
  field('Transaction hash · leave blank to recover it','payment-hash',attempt.claimed_tx_hashes?.length===1?attempt.claimed_tx_hashes[0]:'',{optional:true,placeholder:'0x…'});
  field('Transfer event index · only if needed','payment-index','',{optional:true});
}
async function previewRound(row) {
  const preview=await api('operator/rounds/'+row.round+'/preview');
  dialog('Round '+row.round+' settlement',tig(preview.pot)+' TIG total · '+tig(preview.operator_allocation)+' TIG operator allocation. Review the member allocations below.','Credit this round',async()=>{
    await api('operator/rounds/'+row.round+'/settle',{input_digest:preview.input_digest});await loadOperator();notice('Round allocations credited to the ledger.');
  });
  const list=node('div');for(const [member,amount] of Object.entries(preview.member_allocations)){const value=node('div',undefined,'payment-fact');value.append(address(preview.member_wallets[member] || member),node('span',tig(amount)+' TIG'));list.append(value);}
  if(!list.childNodes.length)list.append(node('p','This complete round has zero member credit. The whole pot goes to the operator.'));
  $('action-fields').append(list);$('action-submit').disabled=!capabilities.settlement_enabled || !preview.preview;
}
async function loadOperator() {
  const [data,payments,custody,funding]=await Promise.all([api('operator/dashboard?limit=50&offset='+operatorOffset),api('operator/withdrawals'),api('operator/custody'),api('operator/funding')]);
  await loadCapabilities();$('operator-login').hidden=true;$('operator-content').hidden=false;$('operator-sign-out').hidden=false;
  const sum=(asset,location,kind)=>data.balances.filter(row=>row.asset===asset&&row.location===location&&(!kind||row.kind===kind)).reduce((total,row)=>total+BigInt(row.balance),0n);
  $('operator-balances').replaceChildren();
  for(const [label,amount,detail] of [['Custody funds',sum('TIG','custody'),'TIG · all recorded accounts'],['Operator funds',sum('TIG','custody','operator'),'TIG · available'],['Network fee funds',sum('NATIVE','custody','operator'),'native token · available'],['Submission balance',sum('TIG','protocol','operator'),'TIG · prepaid operator funds']]){const box=node('article',undefined,'metric');box.append(node('p',label),node('strong',tig(amount)),node('span',detail));$('operator-balances').append(box);}
  const observation=data.observation;$('observation-summary').textContent=observation.initialized?'Observed through block '+observation.latest_seen_height+' · '+observation.missing_heights.length+' listed gaps':'Block collection has not started.';
  const wallet=custody.observation,check=wallet.check;
  const directWallet=!check?.custody_code||check.custody_code==='0x';
  $('custody-health').textContent=!wallet.initialized?'Collection not started':wallet.ready?'Reconciled':'Check required';
  $('custody-health').classList.toggle('active',wallet.ready);
  $('custody-balances').textContent=check?.actual_tig!==null && check?.actual_tig!==undefined ? 'Block '+check.height+' · wallet '+tig(check.actual_tig)+' TIG · ledger '+tig(check.recorded_tig)+' TIG · '+(wallet.ready?'The recorded wallet funds reconcile.':check.healthy?'Waiting for a fresh chain check.':check.reason) : 'Start custody collection before accepting funds. New work waits for a complete, reconciled check once collection is configured.';
  if(!directWallet)$('custody-balances').textContent+=' Smart-account payment recovery is available. New payments require the supported direct-wallet setup.';
  table('unattributed-deposits',custody.unattributed.map(row=>{const actions=node('div');actions.append(button('Credit member',()=>attributeDeposit(row,false)),button('Operator funding',()=>attributeDeposit(row,true)));return[address(row.sender),tig(row.amount),row.block_number,actions];}),4,'No deposits await attribution.');
  $('custody-alerts').replaceChildren();for(const alert of custody.alerts){const row=node('p',alert.kind+' · '+(alert.details.reason||'Review recorded evidence'),'small muted');$('custody-alerts').append(row);}
  const fees=funding.observation,observed=fees.observation;
  $('funding-health').textContent=!fees.initialized?'Collection not started':fees.ready?'Reconciled':'Check required';
  $('funding-health').classList.toggle('active',fees.ready);
  $('funding-detail').textContent=observed?.complete ? 'Block '+observed.height+' · TIG reports '+tig(observed.available)+' TIG · ledger '+tig(fees.recorded)+' TIG. '+(fees.conflicts?.length?'Confirmed top-up history changed. Review the preserved evidence before continuing.':'Only confirmed top-ups fund new submissions.') : 'Start protocol fee collection before preparing a top-up.';
  $('prepare-topup').disabled=!capabilities.funds_enabled||!fees.ready||!directWallet;
  $('prepare-topup').dataset.minimum=funding.policy?.minimum||'';
  table('operator-topups',funding.topups.map(row=>{const actions=node('div');if(row.state==='uncertain')actions.append(button('Check transfer',async()=>showPayment(await api('operator/topups/'+row.id),true)));if(row.state==='awaiting_protocol')actions.append(button('Check TIG credit',async()=>{await api('operator/topups/'+row.id+'/confirm',{});await loadOperator();notice('TIG top-up confirmed and credited once.');}));return[date(row.sent_at),tig(row.amount),status(row.state),actions];}),4,'No fee top-ups recorded.');
  $('pause-work').textContent=capabilities.new_work_paused?'Resume new work':'Pause new work';
  $('settlement-status').textContent=capabilities.settlement_enabled?'Preview each round before crediting its final allocation.':'Live settlement is awaiting verified protocol configuration. Previews remain available when the evidence is complete.';
  table('operator-members',data.members.map(row=>[address(row.wallet),tig(row.available),tig(row.collateral),row.slots+' / 2',row.multiplier+'×',button('Edit multiplier',()=>editMultiplier(row))]),6);
  table('operator-withdrawals',payments.withdrawals.map(row=>{const actions=node('div');if(row.state==='requested')actions.append(button('Approve',()=>reviewWithdrawal(row,true)),button('Reject',()=>reviewWithdrawal(row,false),'danger'));if(row.state==='approved'){const prepare=button('Prepare payment',()=>prepareWithdrawal(row));prepare.disabled=!directWallet;actions.append(prepare,button('Reject',()=>reviewWithdrawal(row,false),'danger'));}if(row.state==='uncertain'){const attempt=payments.attempts.find(value=>value.withdrawal_id===row.id&&!value.outcome);if(attempt)actions.append(button('Check transfer',async()=>showPayment(await api('operator/withdrawal-attempts/'+attempt.id))));}return[address(row.wallet),tig(row.amount),address(row.recipient),status(row.state),actions];}),5);
  table('operator-rounds',data.rounds.map(row=>[row.round,row.credited_blocks+' / '+row.observed_blocks+' observed',row.pending_collateral,row.pot===null?'—':tig(row.pot),row.settled_at?'Settled':'Awaiting finalization',button(row.settled_at?'View allocation':'Preview',()=>previewRound(row))]),6);
  table('operator-holds',data.collateral.map(row=>{const finalize=button('Finalize',async()=>{await api('operator/collateral/'+row.id+'/finalize',{});await loadOperator();});finalize.disabled=!capabilities.settlement_enabled;return[address(row.benchmark_id),row.creation_round,status(row.state),row.handed_over_at?'Yes':'No',tig(row.amount),finalize];}),6);
  for(const [id,items] of [['uncertain-submissions',data.uncertain_submissions.map(row=>[row.kind+' · '+short(row.benchmark_id),'Awaiting authoritative protocol confirmation since '+date(row.sent_at)])],['observation-alerts',data.alerts.map(row=>[row.kind+' · block '+(row.height??'unknown'),date(row.created_at)])]]) {$(id).replaceChildren();for(const [title,text]of items){const item=node('div',undefined,'alert-item');item.append(node('strong',title),node('span',text));$(id).append(item);}if(!items.length)$(id).append(node('p','No records to show.','muted small'));}
  table('multiplier-history',data.multiplier_changes.map(row=>[address(row.member_id),row.old_value+'×',row.new_value+'×',row.reason,date(row.created_at)]),5);
  $('operator-page-number').textContent='Page '+(operatorOffset/50+1);$('operator-newer').disabled=operatorOffset===0;$('operator-older').disabled=data.members.length<50&&data.collateral.length<50;
}
function attributeDeposit(receipt,operator) {
  dialog(operator?'Confirm operator funding':'Credit verified member',tig(receipt.amount)+' TIG received from '+receipt.sender+'. Record how you established ownership.','Credit funds',async()=>{
    const body={tx_hash:receipt.tx_hash,log_index:receipt.log_index,operator_funding:operator,reason:$('attribution-reason').value.trim()};
    if(!operator)body.member_wallet=$('deposit-member-wallet').value.trim();
    await api('operator/custody/attribute-deposit',body);await loadOperator();notice('Deposit attributed. The amount was credited once.');
  });if(!operator)field('Verified member wallet','deposit-member-wallet');field('Ownership check','attribution-reason');
}
function recordReceipt(native) {
  dialog(native?'Record native funding':'Record TIG receipt',native?'Verify incoming network-fee funds. For a transfer sent through a contract, enter its verified call path; leave the path empty for a direct transfer.':'Verify an incoming TIG event. Known member sources are credited automatically; other sources remain for review.','Verify receipt',async()=>{
    const body={tx_hash:$('receipt-hash').value.trim()};
    if(!native){const index=Number($('receipt-index').value);if(!Number.isSafeInteger(index)||index<0)throw Error('Enter a valid transfer event index.');body.log_index=index;}
    if(native){const path=$('receipt-call-path').value.trim();if(path){if(!/^\d+(\.\d+)*$/.test(path))throw Error('Enter the verified call path, for example 5.0.');body.trace_address=path.split('.').map(Number);if(body.trace_address.length>64||body.trace_address.some(i=>!Number.isSafeInteger(i)||i>=2147483648))throw Error('Invalid call path.');}}
    await api('operator/custody/'+(native?'receive-native':'receive-token'),body);await loadOperator();notice('Verified receipt recorded. The observer will refresh wallet reconciliation.');
  });field('Transaction hash','receipt-hash');if(!native)field('Transfer event index','receipt-index');else field('Contract transfer call path · optional','receipt-call-path','',{optional:true,placeholder:'Example: 5.0; empty for direct transfers'});
}
$('record-token-receipt').addEventListener('click',()=>recordReceipt(false));
$('record-native-funding').addEventListener('click',()=>recordReceipt(true));
$('prepare-topup').addEventListener('click',()=>{
  const requestKey=key();
  dialog('Prepare submission fee top-up','Use available operator TIG to fund the pool’s submission balance. The transfer and network fee will be reserved before you send it manually.','Prepare top-up',async()=>{
    const attempt=await api('operator/topups',{request_key:requestKey,amount:units($('topup-amount').value.trim()),fee_limit:units($('topup-gas').value.trim())});
    $('action-dialog').close();await loadOperator();showPayment(attempt,true);return false;
  });field('Amount · TIG','topup-amount',tig($('prepare-topup').dataset.minimum||'0'));field('Maximum reserved operator fee · native token','topup-gas','0.0001');
});
$('refresh-operator').addEventListener('click',()=>run(loadOperator,$('refresh-operator')));
$('pause-work').addEventListener('click',()=>run(async()=>{await api('operator/controls/new-work',{paused:!capabilities.new_work_paused,reason:capabilities.new_work_paused?'Operator resumed new work':'Operator paused new work',event_key:key()});await loadOperator();notice('Work control updated. Existing benchmark recovery remains available.');},$('pause-work')));
for(const [id,change] of [['operator-newer',-50],['operator-older',50]])$(id).addEventListener('click',()=>run(async()=>{operatorOffset=Math.max(0,operatorOffset+change);await loadOperator();}));
$('member-page').hidden=operatorPage;$('operator-page').hidden=!operatorPage;
for(const link of document.querySelectorAll('nav a'))if(new URL(link.href).pathname===location.pathname)link.setAttribute('aria-current','page');
await run(loadCapabilities);
