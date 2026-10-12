"""Root maintenance must leave collector-owned locks writable by collectors."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from pool_manager.pool_v2.protocol import ProtocolDataError
from pool_manager.pool_v2.spool import Spool, spool_lock, pruning_lock, _control_file


class SpoolLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='spool-lock-permissions-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / 'spool'
        self.directory.mkdir()

    def test_root_maintenance_preserves_collector_owner_and_access(self):
        uid, gid = os.geteuid(), os.getegid()
        if uid == 0:
            uid, gid = 59999, 59999
            self.root.chmod(0o755)
            os.chown(self.directory, uid, gid)
        for name, acquire in (('.spool.lock', spool_lock), ('.pruning.lock', pruning_lock)):
            with acquire(self.directory):
                info = (self.directory / name).stat()
                self.assertEqual((info.st_uid, info.st_gid, info.st_mode & 0o777), (uid, gid, 0o600))
        code = ('import os,sys,fcntl;uid=int(sys.argv[1]);gid=int(sys.argv[2]);'
            'os.setgroups([]) if os.geteuid()==0 else None;'
            'os.setgid(gid) if os.geteuid()==0 else None;'
            'os.setuid(uid) if os.geteuid()==0 else None;'
            'files=[open(p,"a") for p in sys.argv[3:]];'
            '[fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB) for f in files]')
        subprocess.run(['/usr/bin/python3', '-c', code, str(uid), str(gid),
            str(self.directory / '.spool.lock'), str(self.directory / '.pruning.lock')], check=True, timeout=10)

    def test_symlink_locks_are_refused(self):
        target = self.root / 'unchanged'
        target.write_text('unchanged')
        for name, acquire in (('.spool.lock', spool_lock), ('.pruning.lock', pruning_lock)):
            (self.directory / name).symlink_to(target)
            with self.assertRaises(OSError):
                with acquire(self.directory):
                    self.fail('symlink lock accepted')
        self.assertEqual(target.read_text(), 'unchanged')

    def test_hardlinked_locks_are_refused(self):
        target = self.root / 'unchanged'
        target.write_text('unchanged')
        for name, acquire in (('.spool.lock', spool_lock), ('.pruning.lock', pruning_lock)):
            os.link(target, self.directory / name)
            with self.assertRaises(ProtocolDataError):
                with acquire(self.directory):
                    self.fail('hardlinked lock accepted')
        self.assertEqual(target.read_text(), 'unchanged')

    def test_root_initialization_preserves_collector_directory_owner(self):
        uid, gid = os.geteuid(), os.getegid()
        if uid == 0:
            uid, gid = 59999, 59999
            os.chown(self.directory, uid, gid)
        Spool(self.directory)
        for name in ('chunks', 'pending', 'recorded', 'writing'):
            info = (self.directory / name).stat()
            self.assertEqual((info.st_uid, info.st_gid), (uid, gid))

    def test_root_gc_journal_is_writable_by_the_collector(self):
        uid, gid = os.geteuid(), os.getegid()
        if uid == 0:
            uid, gid = 59999, 59999
            self.root.chmod(0o755)
            os.chown(self.directory, uid, gid)
        with spool_lock(self.directory):
            _control_file(self.directory, '.gc-pins')
            _control_file(self.directory, '.gc-active')
        for name in ('.gc-pins', '.gc-active'):
            info = (self.directory / name).stat()
            self.assertEqual((info.st_uid, info.st_gid, info.st_mode & 0o777), (uid, gid, 0o600))
        code = ('import os,sys;uid=int(sys.argv[1]);gid=int(sys.argv[2]);'
            'os.setgroups([]) if os.geteuid()==0 else None;'
            'os.setgid(gid) if os.geteuid()==0 else None;'
            'os.setuid(uid) if os.geteuid()==0 else None;'
            'f=open(sys.argv[3],"a");f.write("[]\\n");f.close()')
        subprocess.run(['/usr/bin/python3', '-c', code, str(uid), str(gid),
            str(self.directory / '.gc-pins')], check=True, timeout=10)
        self.assertEqual((self.directory / '.gc-pins').read_text(), '[]\n')

    def test_unsafe_control_files_are_not_truncated(self):
        target = self.root / 'unchanged'
        target.write_text('unchanged')
        (self.directory / '.gc-pins').symlink_to(target)
        os.link(target, self.directory / '.gc-active')
        for name in ('.gc-pins', '.gc-active'):
            with self.assertRaises((OSError, ProtocolDataError)):
                _control_file(self.directory, name)
        self.assertEqual(target.read_text(), 'unchanged')
