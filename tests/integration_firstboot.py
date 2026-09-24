"""Opt-in chroot integration test for first-boot provisioning.

Run only against an explicitly prepared rootfs copy, for example:
  sudo python tests/integration_firstboot.py --root /tmp/m17s-firstboot-test-image
The rootfs must contain a `.m17s-test-root` marker. This test never mounts or
modifies the host filesystem and does not create host accounts.
"""
from __future__ import annotations

import argparse
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from m17s_armbian.firstboot import ProvisionConfig, SystemBackend, provision


class ChrootBackend(SystemBackend):
    def __init__(self, root: Path):
        self.root = root

    def run(self, command, **kwargs):
        command = list(command)
        if command[:2] == ["hostnamectl", "set-hostname"]:
            (self.root / "etc/hostname").write_text(command[2] + "\n", encoding="ascii")
            return None
        if kwargs.get("input") is not None:
            return subprocess.run(["chroot", str(self.root), *command], check=True, **kwargs)
        return subprocess.run(["chroot", str(self.root), *command], check=True, **kwargs)

    def account(self, username):
        # The host pwd database must never be used to decide this test's user.
        passwd_file = self.root / "etc/passwd"
        if not passwd_file.exists():
            return None
        for line in passwd_file.read_text(encoding="utf-8").splitlines():
            fields = line.split(":")
            if len(fields) >= 7 and fields[0] == username:
                uid, gid = int(fields[2]), int(fields[3])
                home = fields[5]
                if not home.startswith("/"):
                    raise RuntimeError("rootfs passwd home must be absolute")
                return pwd.struct_passwd((username, "x", uid, gid, "", str(self.root / home.lstrip("/")), fields[6]))
        return None


def _safe_root(value: str) -> Path:
    supplied = Path(value)
    if supplied.is_symlink():
        raise ValueError("--root itself may not be a symlink")
    root = supplied.resolve()
    if root == Path("/") or root.parent != Path("/tmp") or not root.is_dir():
        raise ValueError("--root must be an existing directory")
    if not root.name.startswith("m17s-firstboot-test-"):
        raise ValueError("--root must be under the explicit m17s-firstboot-test-* namespace")
    marker = root / ".m17s-test-root"
    if marker.is_symlink() or not marker.is_file():
        raise ValueError("--root is missing .m17s-test-root safety marker")
    return root


class FirstbootChrootTests(unittest.TestCase):
    root: Path
    ssh: bool

    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise unittest.SkipTest("requires root for chroot and real account tools")
        if shutil.which("chroot") is None:
            raise unittest.SkipTest("chroot is unavailable")
        cls.backend = ChrootBackend(cls.root)
        cls.keydir = Path(tempfile.mkdtemp(prefix="m17s-firstboot-test-key-"))
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(cls.keydir / "id_ed25519")], check=True)
        public = (cls.keydir / "id_ed25519.pub").read_text(encoding="ascii").strip()
        provision(ProvisionConfig("m17s-integration", "m17stest", (public,)), backend=cls.backend)
        cls.public = public

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.keydir, ignore_errors=True)

    def test_user_sudo_permissions_root_lock_and_marker(self):
        root = self.root
        passwd = (root / "etc/passwd").read_text()
        group = (root / "etc/group").read_text()
        shadow = (root / "etc/shadow").read_text()
        self.assertIn("m17stest:", passwd)
        self.assertRegex(group, re.compile(r"^sudo:[^\n]*m17stest(?:,|$)", re.MULTILINE), msg=group)
        self.assertRegex(shadow, r"^root:!", msg=shadow)
        authorized = root / "home/m17stest/.ssh/authorized_keys"
        self.assertEqual(authorized.read_text().strip(), self.public)
        self.assertEqual((authorized.stat().st_mode & 0o777), 0o600)
        self.assertEqual((authorized.parent.stat().st_mode & 0o777), 0o700)
        self.assertTrue((root / "etc/m17s-armbian/provisioned").is_file())

    def test_key_only_sudoers(self):
        sudoers = self.root / "etc/sudoers.d/m17stest"
        self.assertEqual(sudoers.read_text(), "m17stest ALL=(ALL) NOPASSWD: ALL\n")
        self.assertEqual(sudoers.stat().st_mode & 0o777, 0o440)

    def test_optional_sshd_public_key_login(self):
        if not self.ssh:
            self.skipTest("pass --ssh to run the isolated sshd login check")
        sshd = self.root / "usr/sbin/sshd"
        if not sshd.exists():
            self.skipTest("rootfs has no sshd")
        (self.root / "run/sshd").mkdir(parents=True, exist_ok=True)
        port = "22222"
        proc = subprocess.Popen(["chroot", str(self.root), "/usr/sbin/sshd", "-D", "-e", "-p", port,
                                 "-o", "UsePAM=yes", "-o", "PasswordAuthentication=no", "-o", "PermitRootLogin=no",
                                 "-o", "ListenAddress=127.0.0.1"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for _ in range(30):
                with socket.socket() as sock:
                    if sock.connect_ex(("127.0.0.1", int(port))) == 0:
                        break
                time.sleep(0.1)
            result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                                     "-o", "UserKnownHostsFile=/dev/null", "-i", str(self.keydir / "id_ed25519"),
                                     "-p", port, "m17stest@127.0.0.1", "id && sudo -n true"],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--ssh", action="store_true")
    args = parser.parse_args()
    try:
        root = _safe_root(args.root)
    except ValueError as exc:
        parser.error(str(exc))
    FirstbootChrootTests.root = root
    FirstbootChrootTests.ssh = args.ssh
    return unittest.main(module=__name__, argv=["integration_firstboot"], exit=False).result.wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
