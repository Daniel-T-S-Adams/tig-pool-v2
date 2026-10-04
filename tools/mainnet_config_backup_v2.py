#!/usr/bin/env python3
"""Export encrypted settings and exact deployed application, without DB claims."""
from datetime import datetime, timezone
import fcntl
import grp
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from mainnet_backup_v2 import atomic_json, digest


def main():
    if os.geteuid() != 0:
        raise SystemExit('run as root on the designated primary host')
    os.umask(0o077)
    config = Path('/etc/innopool-v2-mainnet')
    root = Path('/opt/innopool-v2-mainnet')
    exports = Path('/var/lib/innopool-v2-mainnet-backups')
    with Path('/run/innopool-config-backup.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if shutil.disk_usage(exports).free < 25 * 1024**3:
            raise RuntimeError('disk reserve too low')
        deployment = json.loads((config / 'deployment.json').read_text())
        application = json.loads((config / 'web-api-release.json').read_text())
        if application['pool_commit'] != deployment['pool_commit']:
            raise ValueError('application and deployment disagree')
        archive = root / 'artifacts' / (application['pool_commit'] + '.tar.gz')
        if digest(archive) != application['archive_sha256']:
            raise ValueError('application archive checksum mismatch')
        stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')
        name = 'configuration-daily-' + stamp
        pending = exports / ('.preparing-' + name)
        pending.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(prefix='innopool-config-') as private:
            settings = Path(private) / 'settings.tar'
            with tarfile.open(settings, 'w') as saved:
                saved.add(config, arcname='config')
                saved.add('/etc/nginx', arcname='web/nginx')
                saved.add('/etc/systemd/system/nginx.service.d', arcname='web/nginx-systemd')
                saved.add(root / 'operations', arcname='operations')
                for unit in Path('/etc/systemd/system').glob('innopoolv2mainnet*'):
                    if unit.is_file() and not unit.is_symlink():
                        saved.add(unit, arcname='units/' + unit.name)
            subprocess.run(['openssl', 'cms', '-encrypt', '-binary', '-aes-256-cbc',
                '-outform', 'DER', '-in', str(settings), '-out', str(pending / 'settings.tar.cms'),
                str(config / 'backup-recipient.crt')], check=True, capture_output=True, timeout=60)
        shutil.copyfile(archive, pending / 'application-source.tar.gz')
        shutil.copyfile(config / 'web-api-release.json', pending / 'application-release.json')
        with tarfile.open(pending / 'public-web-assets.tar.gz', 'w:gz') as saved:
            for path in sorted((root / 'web/assets').glob('*/*')):
                if not path.is_file() or path.is_symlink() or digest(path) != path.parent.name:
                    raise ValueError('public asset is not an immutable hash')
                saved.add(path, arcname=str(path.relative_to(root / 'web')))
        record = {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'supplement': name,
            'source_host': '46.62.249.188', 'application_commit': application['pool_commit'],
            'configuration_encrypted': True, 'database_snapshot_advanced': False,
            'files': {p.name: digest(p) for p in pending.iterdir() if p.is_file()}}
        atomic_json(pending / 'manifest.json', record)
        group = grp.getgrnam('innopoolbackup').gr_gid
        for path in [pending, *pending.iterdir()]:
            os.chown(path, 0, group)
            path.chmod(0o750 if path.is_dir() else 0o640)
        pending.rename(exports / name)
        atomic_json(exports / 'latest-configuration.json', record)
        os.chown(exports / 'latest-configuration.json', 0, group)
        (exports / 'latest-configuration.json').chmod(0o640)
        print(json.dumps(record), flush=True)


if __name__ == '__main__':
    main()
