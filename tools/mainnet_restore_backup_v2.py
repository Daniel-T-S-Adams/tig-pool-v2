"""Restore the off-host physical backup and replay post-backup WAL, in isolation."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib,json,os,shutil,subprocess,tarfile,time
os.umask(0o077)
CONFIG=Path('/etc/innopool-v2-mainnet')
ROOT=Path('/var/lib/innopool-v2-mainnet/primary-continuous')
config=json.loads((CONFIG/'continuous-backup.json').read_text())
seal=json.loads((ROOT/'rehearsal-seal.json').read_text())
record=json.loads((ROOT/'latest.json').read_text())
assert seal['snapshot']==record['snapshot']
source=ROOT/'base'/record['snapshot']
assert source.parent==ROOT/'base' and source.is_dir() and not source.is_symlink()
for name,expected in record['files'].items():
 assert name in ('base.tar.gz','pg_wal.tar.gz','backup_manifest')
 with (source/name).open('rb') as stream: assert hashlib.file_digest(stream,'sha256').hexdigest()==expected
assert shutil.disk_usage(ROOT).free > seal['database_bytes']+30*1024**3
stamp=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')
target=ROOT/'.drills'/stamp
assert not target.exists()
target.mkdir(parents=True,mode=0o700)
with tarfile.open(source/'base.tar.gz') as archive:
 assert all(item.isfile() or item.isdir() for item in archive.getmembers()),'custom tablespaces require separate review'
 archive.extractall(target,filter='data')
with tarfile.open(source/'pg_wal.tar.gz') as archive:
 assert all(item.isfile() or item.isdir() for item in archive.getmembers())
 archive.extractall(target/'pg_wal',filter='data')
shutil.copyfile(source/'backup_manifest',target/'backup_manifest')
image=config['postgres_image']
subprocess.run(['docker','run','--rm','--network=none','--read-only','--user=0:0','--cap-drop=ALL','--cpus=2','--memory=768m','--memory-swap=768m','--pids-limit=64','--entrypoint=/usr/lib/postgresql/18/bin/pg_verifybackup','--mount',f'type=bind,src={target},dst=/restore,readonly',image,'/restore'],check=True,timeout=7200)
# This copy has no application, no external network, no TCP listener and no
# replication connection. Never install its credentials into the recovery API.
(target/'postgresql.auto.conf').write_text('')
(target/'pg_hba.conf').write_text('local all all trust\n')
(target/'recovery.signal').touch()
for directory,dirs,files in os.walk(target):
 os.chown(directory,999,999)
 for name in files: os.chown(Path(directory)/name,999,999)
archive_wal=target.parent/(stamp+'-wal')
archive_wal.mkdir(mode=0o700)
for path in (ROOT/'wal').iterdir():
 if len(path.name)==24 and record['first_wal_segment'] <= path.name <= seal['wal_segment']:
  shutil.copyfile(path,archive_wal/path.name)
  os.chown(archive_wal/path.name,999,999)
os.chown(archive_wal,999,999)
assert (archive_wal/seal['wal_segment']).is_file()
name='innopool-v2-pitr-rehearsal'
command=['docker','run','-d','--name',name,'--network=none','--read-only','--user=999:999','--cap-drop=ALL','--security-opt=no-new-privileges','--cpus=1','--memory=1g','--memory-swap=1g','--pids-limit=128','--shm-size=128m','--tmpfs=/tmp:rw,nosuid,size=128m','--mount',f'type=bind,src={target},dst=/restore','--mount',f'type=bind,src={archive_wal},dst=/archive,readonly','--entrypoint=postgres',image,'-D','/restore','-c',"listen_addresses=",'-c','unix_socket_directories=/tmp','-c','shared_buffers=128MB','-c',"restore_command=cp /archive/%f %p",'-c','recovery_target_name='+seal['restore_point'],'-c','recovery_target_action=pause','-c','default_transaction_read_only=on']
subprocess.run(command,check=True,stdout=subprocess.DEVNULL)
def sql(query):
 return subprocess.run(['docker','exec',name,'psql','-h','/tmp','-U','postgres','-d','innopool_v2_mainnet','-v','ON_ERROR_STOP=1','-At','-c',query],capture_output=True,text=True,check=True,timeout=90).stdout.strip()
try:
 deadline=time.monotonic()+900
 while True:
  try:
   if sql("SELECT pg_is_in_recovery() AND pg_is_wal_replay_paused()")=='t': break
  except subprocess.CalledProcessError: pass
  if time.monotonic()>deadline: raise RuntimeError('isolated restore did not reach the requested WAL point')
  time.sleep(2)
 marker=sql("SELECT payload FROM backup_verification.probes WHERE name='"+seal['restore_point']+"'")
 assert marker==seal['probe_payload'],'post-backup WAL probe missing'
 restored=json.loads(sql(seal['financial_state_sql']))
 assert restored==seal['financial_state'],'financial state differs from recorded source'
 # Journal conservation and reconstructed balances in the restored database.
 assert sql("SELECT count(*) FROM (SELECT journal_id,asset,sum(amount) FROM pool_v2.entries GROUP BY journal_id,asset HAVING sum(amount)<>0) bad")=='0'
 assert sql("SELECT count(*) FROM pool_v2.accounts a LEFT JOIN (SELECT account_id,sum(amount) amount FROM pool_v2.entries GROUP BY account_id) e ON e.account_id=a.id WHERE a.balance<>coalesce(e.amount,0)")=='0'
 result={'verified_at_utc':datetime.now(timezone.utc).isoformat(),'snapshot':record['snapshot'],'restore_point':seal['restore_point'],'source_host':'46.62.249.188','recovery_host':'2.28.230.81','data_and_wal_checksums_verified':True,'post_backup_transaction_replayed':True,'financial_state_matches':True,'ledger_audit':[],'recovery_target_reached_and_paused':True,'network':'none','public_ports':[],'live_databases_replaced':False,'credentials_activated':False,'real_funded_mainnet_state_tested':False}
 (ROOT/'restore-rehearsal.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps(result),flush=True)
finally:
 subprocess.run(['docker','stop','--time','30',name],check=True,stdout=subprocess.DEVNULL,timeout=60)
# Only remove this freshly created, proven disposable database copy.
subprocess.run(['docker','rm',name],check=True,stdout=subprocess.DEVNULL)
assert target.parent==ROOT/'.drills' and not target.is_symlink()
shutil.rmtree(target)
shutil.rmtree(archive_wal)
