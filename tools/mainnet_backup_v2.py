#!/usr/bin/env python3
"""Off-host PostgreSQL 18 backups. Never starts a pool or sends a transaction.

Run as root on the recovery host with a protected, reviewed JSON configuration.
The image is pinned by digest; replication travels through a separate restricted
SSH tunnel. Database files and the password file stay private on this host.
"""
from datetime import datetime, timezone
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time


STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z\Z")
WAL = re.compile(r"[0-9A-F]{24}\Z")
GIB = 1024**3


def sync_directory(path):
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.pending')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def docker(config, mounts, args, *, network='none', memory='768m', name=None, cpus='2'):
    command = ['docker', 'run', '--rm', '--read-only', '--cap-drop=ALL',
               '--security-opt=no-new-privileges', '--user=0:0', '--cpus=' + cpus,
               '--memory=' + memory, '--memory-swap=' + memory, '--pids-limit=64',
               '--network=' + network, '--tmpfs=/tmp:rw,noexec,nosuid,size=32m']
    if name:
        command += ['--name', name]
    for source, target, mode in mounts:
        command += ['--mount', f'type=bind,src={source},dst={target}' + (',readonly' if mode == 'ro' else '')]
    command += ['--entrypoint=' + args[0], config['postgres_image'], *args[1:]]
    return command


def checked(command, timeout):
    # No credential is passed on argv. Avoid dumping subprocess output that
    # could include configuration; diagnostics stay in the protected journal.
    subprocess.run(command, check=True, timeout=timeout)


def validated_records(root):
    records = []
    for directory in sorted(Path(root).iterdir()):
        if not STAMP.fullmatch(directory.name):
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('unsafe backup directory')
        record = json.loads((directory / 'verified.json').read_text())
        if record['snapshot'] != directory.name or not record['data_checksums_verified']:
            raise ValueError('invalid backup verification record')
        if not WAL.fullmatch(record['first_wal_segment']):
            raise ValueError('invalid backup WAL boundary')
        if set(record['files']) != {'base.tar.gz', 'pg_wal.tar.gz', 'backup_manifest'}:
            raise ValueError('unexpected backup file set')
        for name, expected in record['files'].items():
            path = directory / name
            if path.is_symlink() or not path.is_file() or digest(path) != expected:
                raise ValueError('retained backup checksum mismatch')
        records.append(record)
    return records


def retention_plan(records, wal_names, keep=3):
    """Only redundant base backups and older WAL on their common timeline.

    Keep at least three complete verified bases. Unknown timelines, partial WAL,
    history files and all protocol evidence are never selected for removal.
    """
    if keep < 3:
        raise ValueError('retain at least three verified base backups')
    records = sorted(records, key=lambda item: item['snapshot'])
    if len({row['snapshot'] for row in records}) != len(records):
        raise ValueError('duplicate backup snapshot')
    if len(records) < keep:
        return [], []
    identities = {row['system_identifier'] for row in records}
    timelines = {row['first_wal_segment'][:8] for row in records}
    if len(identities) != 1 or len(timelines) != 1:
        raise ValueError('cluster or timeline changed; review retention manually')
    retained = records[-keep:]
    floor = min(row['first_wal_segment'] for row in retained)
    return ([row['snapshot'] for row in records[:-keep]],
            [name for name in wal_names if WAL.fullmatch(name)
             and name[:8] == floor[:8] and name < floor])


def basebackup(config):
    root = Path(config['directory'])
    bases, wal = root / 'base', root / 'wal'
    bases.mkdir(mode=0o700, parents=True, exist_ok=True)
    wal.mkdir(mode=0o700, exist_ok=True)
    with (root / 'backup.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if shutil.disk_usage(root).free < config['minimum_free_gib'] * GIB:
            raise RuntimeError('disk reserve too low; existing backups retained')
        stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')
        staging = bases / ('.preparing-' + stamp)
        staging.mkdir(mode=0o700)
        started = time.monotonic()
        mounts = [(staging, '/backup', 'rw'), (config['pgpass'], '/run/pgpass', 'ro')]
        connection = ('host=127.0.0.1 port=' + str(config['tunnel_port']) +
                      ' user=innopool_replication passfile=/run/pgpass'
                      ' application_name=innopool_basebackup connect_timeout=10')
        checked(docker(config, mounts, ['pg_basebackup', '--dbname=' + connection,
            '--pgdata=/backup', '--format=tar', '--compress=client-gzip:1',
            '--wal-method=stream', '--checkpoint=spread', '--max-rate=16M',
            '--manifest-checksums=SHA256', '--no-password'], network='host',
            name='innopool-v2-basebackup'), timeout=14400)
        # PG18 validates tar data directly. Extract only its small WAL archive
        # and parse the exact recovery range, without a second full DB copy.
        checked(docker(config, [(staging, '/backup', 'ro')],
            ['/usr/lib/postgresql/18/bin/pg_verifybackup', '--format=tar',
             '--no-parse-wal', '/backup']), timeout=14400)
        manifest = json.loads((staging / 'backup_manifest').read_text())
        ranges = manifest['WAL-Ranges']
        if len(ranges) != 1 or config['wal_segment_bytes'] != 16 * 1024**2:
            raise RuntimeError('unexpected WAL layout; retained for inspection')
        with tempfile.TemporaryDirectory(prefix='.verify-wal-', dir=root) as directory:
            with tarfile.open(staging / 'pg_wal.tar.gz') as archive:
                if not all(item.isfile() or item.isdir() for item in archive.getmembers()):
                    raise ValueError('unexpected WAL archive members')
                archive.extractall(directory, filter='data')
            value = ranges[0]
            checked(docker(config, [(directory, '/wal', 'ro')],
                ['/usr/lib/postgresql/18/bin/pg_waldump', '--quiet', '--path=/wal',
                 '--timeline=' + str(value['Timeline']), '--start=' + value['Start-LSN'],
                 '--end=' + value['End-LSN']]), timeout=1800)
        high, low = (int(part, 16) for part in ranges[0]['Start-LSN'].split('/'))
        first = f"{ranges[0]['Timeline']:08X}{high:08X}{low // config['wal_segment_bytes']:08X}"
        files = {name: digest(staging / name)
                 for name in ('base.tar.gz', 'pg_wal.tar.gz', 'backup_manifest')}
        record = {'snapshot': stamp, 'completed_at_utc': datetime.now(timezone.utc).isoformat(),
            'source_host': config['source_host'], 'postgres_image': config['postgres_image'],
            'system_identifier': str(manifest['System-Identifier']), 'first_wal_segment': first,
            'data_checksums_verified': True, 'wal_parse_verified': True,
            'actual_restore_tested': False, 'files': files,
            'archive_bytes': sum((staging / name).stat().st_size for name in files),
            'duration_seconds': round(time.monotonic() - started, 1)}
        atomic_json(staging / 'verified.json', record)
        os.rename(staging, bases / stamp)
        sync_directory(bases)
        atomic_json(root / 'latest.json', record)
        # Verify every retained archive before allowing any pruning. This removes
        # duplicate recovery copies only, never unresolved protocol evidence.
        records = validated_records(bases)
        old_bases, old_wal = retention_plan(records, [p.name for p in wal.iterdir()])
        for name in old_bases:
            shutil.rmtree(bases / name)
        for name in old_wal:
            (wal / name).unlink()
        atomic_json(root / 'retention.json', {'at_utc': datetime.now(timezone.utc).isoformat(),
            'removed_base_snapshots': old_bases, 'removed_wal_segments': len(old_wal),
            'kept_base_snapshots': [r['snapshot'] for r in records if r['snapshot'] not in old_bases]})
        print(json.dumps(record), flush=True)


def receive(config):
    root = Path(config['directory'])
    wal = root / 'wal'
    wal.mkdir(parents=True, mode=0o700, exist_ok=True)
    command = docker(config, [(wal, '/wal', 'rw'), (config['pgpass'], '/run/pgpass', 'ro')],
        ['pg_receivewal', '--directory=/wal', '--dbname=host=127.0.0.1 port=' +
         str(config['tunnel_port']) + ' user=innopool_replication passfile=/run/pgpass'
         ' application_name=innopool_wal_archive connect_timeout=10',
         '--slot=innopool_germany_archive', '--synchronous', '--status-interval=5',
         '--no-password'], network='host', memory='128m', name='innopool-v2-receivewal', cpus='0.25')
    os.execvp(command[0], command)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['basebackup', 'receive'])
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('run as root on the designated recovery host')
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    if not re.fullmatch(r'postgres@sha256:[a-f0-9]{64}', config['postgres_image']):
        raise SystemExit('a pinned PostgreSQL image digest is required')
    if not 1024 < config['tunnel_port'] < 65536 or config['minimum_free_gib'] < 25:
        raise SystemExit('invalid backup safety configuration')
    (basebackup if args.action == 'basebackup' else receive)(config)


if __name__ == '__main__':
    main()
