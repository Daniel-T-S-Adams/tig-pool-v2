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


def validated_records(root, *, checksums=True):
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
            if path.is_symlink() or not path.is_file() or (checksums and digest(path) != expected):
                raise ValueError('retained backup checksum mismatch')
        records.append(record)
    return records


def base_catalogue(root, archive=None, *, checksums=True):
    """One catalogue across the volume and optional older-base archive."""
    records, locations = {}, {}
    directories = [Path(root)] + ([Path(archive)] if archive is not None else [])
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('base archive directory is missing or unsafe')
        for record in validated_records(directory, checksums=checksums):
            stamp = record['snapshot']
            if stamp in records and records[stamp] != record:
                raise ValueError('conflicting copies of a base snapshot')
            records[stamp] = record
            locations.setdefault(stamp, []).append(directory / stamp)
    return [records[key] for key in sorted(records)], locations


def locate_base(root, snapshot, archive=None):
    if not STAMP.fullmatch(snapshot):
        raise ValueError('invalid backup snapshot')
    _, locations = base_catalogue(root, archive, checksums=False)
    if snapshot not in locations:
        raise ValueError('verified base snapshot is unavailable')
    return locations[snapshot][0]


def copy_verified_base(source, destination, record, *, minimum_free_gib):
    source, destination = Path(source), Path(destination)
    if destination.exists():
        found = [r for r in validated_records(destination.parent) if r['snapshot'] == record['snapshot']]
        if found != [record]:
            raise ValueError('different archive already occupies the destination')
        return
    required = sum((source / name).stat().st_size for name in record['files'])
    if shutil.disk_usage(destination.parent).free < required + minimum_free_gib * GIB:
        raise RuntimeError('older-base archive disk reserve is too low')
    staging = destination.with_name('.copying-' + destination.name)
    staging.mkdir(mode=0o700)
    for name, expected in record['files'].items():
        target = staging / name
        with (source / name).open('rb') as incoming, target.open('xb') as outgoing:
            unflushed = 0
            while chunk := incoming.read(8 * 1024**2):
                outgoing.write(chunk)
                unflushed += len(chunk)
                if unflushed >= 128 * 1024**2:
                    outgoing.flush(); os.fsync(outgoing.fileno())
                    if hasattr(os, 'posix_fadvise'):
                        os.posix_fadvise(outgoing.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                    unflushed = 0
            outgoing.flush(); os.fsync(outgoing.fileno())
        target.chmod(0o600)
        with target.open('rb') as saved:
            os.fsync(saved.fileno())
        if digest(target) != expected:
            raise ValueError('copied base archive checksum mismatch')
    shutil.copyfile(source / 'verified.json', staging / 'verified.json')
    with (staging / 'verified.json').open('rb') as saved:
        os.fsync(saved.fileno())
    if json.loads((staging / 'verified.json').read_text()) != record:
        raise ValueError('copied backup verification record differs')
    sync_directory(staging)
    os.rename(staging, destination)
    sync_directory(destination.parent)


def maintain_bases(root, archive, *, minimum_free_gib=35, keep=3):
    """Keep the newest base on the volume and older retained bases on root.

    Every retained copy is verified before originals or required WAL can go.
    Snapshot identities and archive bytes are unchanged by relocation.
    """
    root, archive = Path(root), Path(archive)
    bases, wal = root / 'base', root / 'wal'
    records, locations = base_catalogue(bases, archive)
    if len(records) < keep:
        raise ValueError('fewer than three verified bases; do not relocate or prune')
    old_bases, old_wal = retention_plan(records, [p.name for p in wal.iterdir()], keep=keep)
    retained = [r for r in records if r['snapshot'] not in old_bases]
    newest = retained[-1]['snapshot']
    desired = {}
    for record in retained:
        stamp = record['snapshot']
        target = (bases if stamp == newest else archive) / stamp
        if target not in locations[stamp]:
            copy_verified_base(locations[stamp][0], target, record, minimum_free_gib=minimum_free_gib)
        desired[stamp] = target
    # All three final locations now hold verified immutable bytes. Delete only
    # redundant copies, superseded bases and the WAL selected by the same policy.
    removed_duplicates = []
    for stamp, paths in locations.items():
        for path in paths:
            if stamp not in desired or path != desired[stamp]:
                shutil.rmtree(path)
                if stamp in desired:
                    removed_duplicates.append(str(path))
    for name in old_wal:
        (wal / name).unlink()
    sync_directory(bases); sync_directory(archive); sync_directory(wal)
    result = {'at_utc': datetime.now(timezone.utc).isoformat(),
        'removed_base_snapshots': old_bases, 'removed_wal_segments': len(old_wal),
        'kept_base_snapshots': [r['snapshot'] for r in retained],
        'newest_base_on_volume': newest, 'older_base_archive': str(archive),
        'removed_duplicate_locations': removed_duplicates}
    atomic_json(root / 'retention.json', result)
    return result


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
        archive = Path(config['base_archive_directory']) if config.get('base_archive_directory') else None
        if archive is not None:
            records, _ = base_catalogue(bases, archive, checksums=False)
            if len(records) < 3:
                raise RuntimeError('verified base catalogue is incomplete')
            forecast = max(r['archive_bytes'] for r in records) * 1.15
            for directory in (bases, archive):
                if shutil.disk_usage(directory).free < config['minimum_free_gib'] * GIB + forecast:
                    raise RuntimeError('base backup filesystem reserve is too low')
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
            # Keep `archive` for the older-base directory; this is the WAL tarball.
            with tarfile.open(staging / 'pg_wal.tar.gz') as wal_tarball:
                if not all(item.isfile() or item.isdir() for item in wal_tarball.getmembers()):
                    raise ValueError('unexpected WAL archive members')
                wal_tarball.extractall(directory, filter='data')
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
        if archive is not None:
            maintain_bases(root, archive, minimum_free_gib=config['minimum_free_gib'])
            print(json.dumps(record), flush=True)
            return
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
