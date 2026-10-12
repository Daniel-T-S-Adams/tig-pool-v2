#!/usr/bin/env python3
"""Bounded, verified snapshots of protocol evidence awaiting database import.

Export through a dedicated forced SSH command. Receive on the recovery host.
No pool service is started, no database is changed, and primary evidence is never
deleted. Retire only this tool's previous snapshots after a verified replacement
and acknowledgment of its database WAL barrier by the recovery receiver.
"""
import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from mainnet_backup_v2 import atomic_json, sync_directory

KINDS = ('blocks', 'reports', 'custody', 'funding')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
MANIFEST = re.compile(r'[0-9a-f]{32}\.json\.gz\Z')
SNAPSHOT = re.compile(r'[0-9a-f]{32}\Z')
MAX_TOTAL = 1024**3
MAX_FILE = 64 * 1024**2
MAX_MANIFEST = 4 * 1024**2
MAX_MANIFESTS = 10000
MAX_FILES = 100000
MAX_RAW = 64 * 1024**2
FORMAT = 'innopool-pending-evidence-v1'


def now():
    return datetime.now(timezone.utc)


def lsn(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9A-F]{1,8}/[0-9A-F]{1,8}', value):
        raise ValueError('invalid WAL boundary')
    high, low = (int(part, 16) for part in value.split('/'))
    return (high << 32) + low


def barrier_valid(record):
    if (not isinstance(record, dict) or not re.fullmatch(r'[0-9]+', str(record.get('system_identifier', '')))
            or record.get('recovery_acknowledged') is not True
            or lsn(record.get('recovery_flush_lsn')) < lsn(record.get('source_lsn'))):
        raise ValueError('pending snapshot lacks durable recovery WAL acknowledgment')


@contextmanager
def lock(path, *, nonblocking=False):
    with Path(path).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield


def safe_file(path, maximum=MAX_FILE):
    path = Path(path)
    # Reject symlink directories too; the archive contains no links or devices.
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError('unsafe evidence file')
    if path.stat().st_size > maximum:
        raise ValueError('evidence file exceeds snapshot limit')
    return path


def read_gzip(path, maximum=MAX_RAW):
    safe_file(path)
    with gzip.open(path, 'rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('expanded evidence exceeds snapshot limit')
    return raw


def content_hash(path):
    with safe_file(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def references(node, load, *, depth=0):
    if not isinstance(node, dict) or len(node) != 1 or depth > 64:
        raise ValueError('invalid evidence manifest node')
    tag, value = next(iter(node.items()))
    refs = set()
    def digests(values):
        if not isinstance(values, list) or not all(isinstance(d, str) and DIGEST.fullmatch(d) for d in values):
            raise ValueError('invalid evidence digest list')
        if len(values) > MAX_FILES:
            raise ValueError('too many evidence references')
        return values
    if tag == 'dict':
        if not isinstance(value, list) or len(value) > MAX_FILES:
            raise ValueError('invalid dictionary manifest')
        keys = set()
        for pair in value:
            if (not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
                    or pair[0] in keys):
                raise ValueError('invalid dictionary manifest')
            keys.add(pair[0])
            refs.update(references(pair[1], load, depth=depth + 1))
            if len(refs) > MAX_FILES:
                raise ValueError('too many evidence references')
    elif tag == 'pages':
        for digest in digests(value):
            raw = load(digest)
            if not raw or len(raw) % 32:
                raise ValueError('invalid evidence digest page')
            refs.add(digest)
            refs.update(raw[i:i + 32].hex() for i in range(0, len(raw), 32))
            if len(refs) > MAX_FILES:
                raise ValueError('too many evidence references')
    elif tag == 'items':
        for digest in digests(value):
            listing = json.loads(load(digest))
            if not isinstance(listing, list) or not all(isinstance(item, str) for item in listing):
                raise ValueError('invalid evidence item page')
            refs.add(digest)
    elif tag == 'records':
        refs.update(digests(value))
    elif tag == 'blob':
        refs.update(digests([value]))
    elif tag != 'value':
        raise ValueError('unknown evidence manifest node')
    return refs


def chunk_loader(directory):
    def load(digest):
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ValueError('invalid evidence digest')
        raw = read_gzip(directory / 'chunks' / digest[:2] / (digest + '.gz'))
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('evidence chunk checksum mismatch')
        return raw
    return load


def export_snapshot(root, output, wal_barrier, *, maximum=MAX_TOTAL, selected_hook=None):
    root = Path(root)
    files, cached, counts = {}, {}, {}
    cached_bytes = 0
    captured = now().isoformat()
    with ExitStack() as pruning:
        # Matching maintenance locks protect the selected chunks during transfer;
        # publication locks are held only while selecting existing manifests.
        for kind in KINDS:
            directory = root / kind
            if (directory.is_symlink() or not directory.is_dir()
                    or any((directory / part).is_symlink() or not (directory / part).is_dir()
                           for part in ('pending', 'chunks'))):
                raise ValueError('required protocol spool is missing or unsafe')
            pruning.enter_context(lock(directory / '.pruning.lock'))
        with ExitStack() as publication:
            for kind in KINDS:
                publication.enter_context(lock(root / kind / '.spool.lock'))
            for kind in KINDS:
                paths = sorted((root / kind / 'pending').glob('*.json.gz'))
                counts[kind] = len(paths)
                if sum(counts.values()) > MAX_MANIFESTS:
                    raise ValueError('pending manifest count exceeds snapshot limit')
                for path in paths:
                    if not MANIFEST.fullmatch(path.name):
                        raise ValueError('invalid pending manifest name')
                    safe_file(path, MAX_MANIFEST)
                    name = kind + '/pending/' + path.name
                    cached[name] = path.read_bytes()
                    cached_bytes += len(cached[name])
                    if cached_bytes > maximum:
                        raise ValueError('pending snapshot exceeds transfer limit')
        if selected_hook:
            selected_hook()
        selected_bytes = cached_bytes
        for name, data in cached.items():
            kind = name.split('/')[0]
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
                raw = handle.read(MAX_MANIFEST + 1)
            if len(raw) > MAX_MANIFEST:
                raise ValueError('expanded manifest exceeds snapshot limit')
            saved = json.loads(raw)
            load = chunk_loader(root / kind)
            for digest in references(saved['manifest'], load):
                chunk_name = kind + '/chunks/' + digest[:2] + '/' + digest + '.gz'
                if chunk_name in files:
                    continue
                chunk = safe_file(root / kind / 'chunks' / digest[:2] / (digest + '.gz'))
                selected_bytes += chunk.stat().st_size
                if selected_bytes > maximum:
                    raise ValueError('pending snapshot exceeds transfer limit')
                # Verify every referenced record/blob as well as digest pages.
                load(digest)
                files[chunk_name] = chunk
                if len(files) + len(cached) > MAX_FILES:
                    raise ValueError('snapshot file count exceeds limit')
        entries = {}
        for name, value in {**files, **cached}.items():
            entries[name] = {'bytes': len(value) if isinstance(value, bytes) else value.stat().st_size,
                'sha256': hashlib.sha256(value).hexdigest() if isinstance(value, bytes) else content_hash(value)}
        total = sum(row['bytes'] for row in entries.values())
        if total > maximum:
            raise ValueError('pending snapshot exceeds transfer limit')
        barrier = wal_barrier()
        barrier_valid(barrier)
        record = {'format': FORMAT, 'snapshot': uuid.uuid4().hex,
            'source_captured_at_utc': captured, 'source_completed_at_utc': now().isoformat(),
            'wal_barrier': barrier, 'manifests': counts, 'content_bytes': total, 'files': entries}
        raw_index = json.dumps(record, sort_keys=True).encode()
        if len(raw_index) > MAX_FILE:
            raise ValueError('snapshot index exceeds limit')
        with tarfile.open(fileobj=output, mode='w|') as archive:
            def add(name, raw):
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(raw), 0o600
                archive.addfile(item, io.BytesIO(raw))
            add('snapshot.json', raw_index)
            for name, path in sorted(files.items()):
                item = tarfile.TarInfo(name)
                item.size, item.mode = entries[name]['bytes'], 0o600
                with safe_file(path).open('rb') as handle:
                    archive.addfile(item, handle)
            for name, data in sorted(cached.items()):
                add(name, data)
        return record


def valid_member(name):
    parts = name.split('/')
    if len(parts) == 3 and parts[0] in KINDS and parts[1] == 'pending':
        return bool(MANIFEST.fullmatch(parts[2]))
    if len(parts) == 4 and parts[0] in KINDS and parts[1] == 'chunks':
        digest = parts[3][:-3] if parts[3].endswith('.gz') else ''
        return bool(DIGEST.fullmatch(digest) and parts[2] == digest[:2])
    return False


def extract_snapshot(stream, target, *, maximum=MAX_TOTAL):
    target = Path(target)
    selected, total = set(), 0
    with tarfile.open(fileobj=stream, mode='r|') as archive:
        for item in archive:
            if (not item.isfile() or item.name in selected or
                    (item.name != 'snapshot.json' and not valid_member(item.name))):
                raise ValueError('unsafe or duplicate snapshot member')
            selected.add(item.name)
            total += item.size
            if len(selected) > MAX_FILES + 1 or item.size > MAX_FILE or total > maximum + MAX_FILE:
                raise ValueError('snapshot exceeds extraction limit')
            path = target / item.name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.extractfile(item) as source, path.open('xb') as output:
                shutil.copyfileobj(source, output, length=1024**2)
                output.flush()
                os.fsync(output.fileno())
            path.chmod(0o600)
    return verify_snapshot(target, maximum=maximum)


def verify_snapshot(root, *, maximum=MAX_TOTAL):
    root = Path(root)
    record = json.loads(safe_file(root / 'snapshot.json').read_text())
    if record.get('format') != FORMAT or not SNAPSHOT.fullmatch(record.get('snapshot', '')):
        raise ValueError('invalid pending snapshot identity')
    barrier_valid(record.get('wal_barrier'))
    files = record.get('files')
    if (not isinstance(files, dict) or len(files) > MAX_FILES or
            set(record.get('manifests', {})) != set(KINDS)):
        raise ValueError('invalid pending snapshot index')
    actual = {str(path.relative_to(root)) for path in root.rglob('*') if path.is_file() or path.is_symlink()}
    if actual != set(files) | {'snapshot.json'}:
        raise ValueError('snapshot file set differs from index')
    total, counts, referenced = 0, dict.fromkeys(KINDS, 0), set()
    for name, entry in files.items():
        if not valid_member(name) or not isinstance(entry, dict):
            raise ValueError('invalid snapshot file entry')
        path = safe_file(root / name)
        if path.stat().st_size != entry.get('bytes') or content_hash(path) != entry.get('sha256'):
            raise ValueError('snapshot file checksum mismatch')
        total += path.stat().st_size
        if total > maximum:
            raise ValueError('snapshot exceeds transfer limit')
        kind = name.split('/')[0]
        if '/pending/' in name:
            counts[kind] += 1
            saved = json.loads(read_gzip(path, MAX_MANIFEST))
            load = chunk_loader(root / kind)
            for digest in references(saved['manifest'], load):
                load(digest)
                referenced.add(kind + '/chunks/' + digest[:2] + '/' + digest + '.gz')
    chunks = {name for name in files if '/chunks/' in name}
    if (counts != record['manifests'] or sum(counts.values()) > MAX_MANIFESTS
            or total != record.get('content_bytes') or chunks != referenced):
        raise ValueError('snapshot coverage differs from verified references')
    return record


def publish_snapshot(root, staging, *, system_identifier, keep=2):
    root, staging = Path(root), Path(staging)
    if staging.parent != root or not staging.name.startswith('.receiving-') or staging.is_symlink():
        raise ValueError('snapshot staging must belong to the recovery archive')
    if keep != 2:
        raise ValueError('keep two verified pending snapshots')
    record = verify_snapshot(staging)
    if record['wal_barrier']['system_identifier'] != str(system_identifier):
        raise ValueError('pending snapshot belongs to a different database cluster')
    stamp = datetime.fromisoformat(record['source_captured_at_utc'])
    age = (now() - stamp).total_seconds()
    if age < -60 or age > 600:
        raise ValueError('pending snapshot source is stale')
    previous = []
    snapshots = root / 'snapshots'
    snapshots.mkdir(mode=0o700, exist_ok=True)
    for path in snapshots.iterdir():
        if not SNAPSHOT.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
            raise ValueError('unexpected pending archive directory; review manually')
        old = verify_snapshot(path)
        if (old['snapshot'] != path.name or old['wal_barrier']['system_identifier'] != str(system_identifier)
                or lsn(old['wal_barrier']['source_lsn']) > lsn(record['wal_barrier']['source_lsn'])
                or datetime.fromisoformat(old['source_captured_at_utc']) > stamp):
            raise ValueError('pending archive boundary regressed or changed cluster')
        previous.append((old['source_captured_at_utc'], path))
    destination = snapshots / record['snapshot']
    if destination.exists():
        raise ValueError('duplicate pending snapshot')
    # Ensure the whole directory tree is durable before publishing its pointer.
    for directory, _, _ in os.walk(staging, topdown=False):
        sync_directory(directory)
    os.rename(staging, destination)
    sync_directory(snapshots)
    status = {'format': FORMAT, 'snapshot': record['snapshot'],
        'completed_at_utc': now().isoformat(), 'source_captured_at_utc': record['source_captured_at_utc'],
        'wal_barrier': record['wal_barrier'], 'manifests': record['manifests'],
        'content_bytes': record['content_bytes'], 'all_manifest_references_verified': True,
        'all_content_digests_verified': True, 'source_deletion_enabled': False,
        'index_sha256': content_hash(destination / 'snapshot.json'),
        'retained_snapshots': min(len(previous) + 1, keep)}
    atomic_json(root / 'status.json', status)
    # New snapshot is verified and its WAL covers imports absent from this cut.
    # Remove only tool-owned generations, never primary files or database bases.
    for _, path in sorted(previous)[:max(0, len(previous) + 1 - keep)]:
        shutil.rmtree(path)
    sync_directory(snapshots)
    return status


def restore_snapshot(snapshot, target):
    """Copy a verified pending snapshot into a new, isolated spool tree."""
    record = verify_snapshot(snapshot)
    target = Path(target)
    target.mkdir(mode=0o700)  # Refuse to overwrite an existing/live spool.
    for kind in KINDS:
        for part in ('pending', 'recorded', 'chunks', 'writing'):
            (target / kind / part).mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in record['files']:
        path = target / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(Path(snapshot) / name, path)
        path.chmod(0o600)
    return record


def database_barrier():
    import psycopg2
    password = Path('/etc/innopool-v2-mainnet/postgres-password').read_text().strip()
    with psycopg2.connect(host='127.0.0.1', port=55440, user='postgres', dbname='postgres',
            password=password, connect_timeout=5) as connection:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout='5s'")
            cursor.execute('SELECT system_identifier::text FROM pg_control_system()')
            identifier = cursor.fetchone()[0]
            cursor.execute('SELECT pg_current_wal_insert_lsn()::text')
            boundary = cursor.fetchone()[0]
            deadline = time.monotonic() + 15
            while True:
                cursor.execute("""SELECT r.flush_lsn::text FROM pg_replication_slots s
                    JOIN pg_stat_replication r ON r.pid=s.active_pid
                    WHERE s.slot_name='innopool_germany_archive' AND s.active
                    AND r.application_name='innopool_wal_archive'""")
                row = cursor.fetchone()
                if row and row[0] and lsn(row[0]) >= lsn(boundary):
                    return {'system_identifier': identifier, 'source_lsn': boundary,
                        'recovery_flush_lsn': row[0], 'recovery_acknowledged': True}
                if time.monotonic() >= deadline:
                    raise RuntimeError('recovery has not acknowledged the pending snapshot WAL boundary')
                time.sleep(.25)


def receive(config):
    root = Path(config['directory'])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock(root / 'sync.lock', nonblocking=True):
        # Small compressed chunks still occupy whole filesystem blocks. Budget
        # their allocation/metadata and the index as well as compressed content.
        allocation = MAX_TOTAL + MAX_FILE + (2 * MAX_FILES + 4096) * os.statvfs(root).f_frsize
        if shutil.disk_usage(root).free < (config['minimum_free_gib'] * 1024**3 + allocation):
            raise RuntimeError('disk reserve too low; existing pending snapshots retained')
        base = json.loads(Path(config['base_record']).read_text())
        if base.get('data_checksums_verified') is not True or base.get('wal_parse_verified') is not True:
            raise ValueError('pending evidence requires a verified database base')
        args = ['ssh', '-F', '/dev/null', '-i', config['ssh_key'], '-o', 'IdentitiesOnly=yes',
            '-o', 'BatchMode=yes', '-o', 'ForwardAgent=no', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + config['known_hosts'], '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            'root@' + config['source_host'], 'export-pending-v1']
        with tempfile.TemporaryDirectory(prefix='.receiving-', dir=root) as temporary:
            staging = Path(temporary) / 'snapshot'
            staging.mkdir(mode=0o700)
            # The service's TimeoutStartSec bounds a stalled stream; no credential
            # or remote error output enters the public health record.
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errors)
                try:
                    record = extract_snapshot(process.stdout, staging)
                    process.stdout.close()
                    if process.wait(timeout=15) != 0:
                        raise RuntimeError('pending evidence export failed')
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=15)
            # Move to an owned direct child, as required for atomic publication.
            selected = root / ('.receiving-' + record['snapshot'])
            if selected.exists():
                raise ValueError('pending snapshot staging already exists')
            os.rename(staging, selected)
            try:
                return publish_snapshot(root, selected, system_identifier=base['system_identifier'])
            finally:
                if selected.exists():
                    shutil.rmtree(selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('export', 'receive', 'restore'))
    parser.add_argument('--config', type=Path)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--target', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'export':
        if os.geteuid() != 0 or os.environ.get('SSH_ORIGINAL_COMMAND') != 'export-pending-v1':
            raise SystemExit('export requires the dedicated forced SSH command')
        export_snapshot('/var/lib/innopool-v2-mainnet/spool', sys.stdout.buffer, database_barrier)
    elif args.action == 'receive':
        if os.geteuid() != 0 or args.config is None:
            raise SystemExit('receive requires root and a protected config file')
        config = json.loads(args.config.read_text())
        if config['minimum_free_gib'] < 35 or config['source_host'] != '46.62.249.188':
            raise SystemExit('invalid pending backup safety configuration')
        print(json.dumps(receive(config)))
    else:
        if args.snapshot is None or args.target is None:
            raise SystemExit('restore requires snapshot and new isolated target paths')
        print(json.dumps(restore_snapshot(args.snapshot, args.target)))


if __name__ == '__main__':
    main()
