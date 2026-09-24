import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import os
import subprocess

from m17s_armbian.firstboot import ProvisionConfig, parse_config, provision, validate_authorized_key


def key(kind: str, blob: bytes) -> str:
    return kind + " " + base64.b64encode(blob).decode()


def ssh_blob(kind: bytes, *parts: bytes) -> bytes:
    def s(value):
        return len(value).to_bytes(4, "big") + value
    return s(kind) + b"".join(s(x) for x in parts)


class FirstbootTests(unittest.TestCase):
    def test_parse_valid_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "firstboot.json"
            p.write_text(json.dumps({"hostname": "m17s-box"}))
            self.assertEqual(parse_config(p), ProvisionConfig("m17s-box"))

    def test_rejects_unknown_password_and_injection(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "firstboot.json"
            p.write_text(json.dumps({"hostname": "ok", "password": "secret"}))
            with self.assertRaises(ValueError):
                parse_config(p)
            p.write_text(json.dumps({"hostname": "ok\nowned"}))
            with self.assertRaises(ValueError):
                parse_config(p)

    def test_key_validation_and_options_rejected(self):
        good = key("ssh-ed25519", ssh_blob(b"ssh-ed25519", b"x" * 32))
        self.assertEqual(validate_authorized_key(good), good)
        with self.assertRaises(ValueError):
            validate_authorized_key("restrict " + good)
        with self.assertRaises(ValueError):
            validate_authorized_key("ssh-ed25519 !!!")
        with self.assertRaises(ValueError):
            validate_authorized_key("-----BEGIN OPENSSH PRIVATE KEY-----")

    def test_apply_does_not_run_before_validation(self):
        with self.assertRaises(ValueError):
            provision(ProvisionConfig("bad name", "m17s"), backend=FakeBackend(tempfile.mkdtemp()))

    def test_provision_uses_sudo_and_locks_root(self):
        with tempfile.TemporaryDirectory() as d:
            backend = FakeBackend(d)
            provision(ProvisionConfig("m17s-box", "m17s"), backend=backend, password="a" * 12)
            self.assertIn(("useradd", "--create-home", "--groups", "sudo", "--shell", "/bin/bash", "m17s"), backend.commands)
            self.assertIn(("passwd", "-l", "root"), backend.commands)
            self.assertTrue(Path(d, "etc/m17s-armbian/provisioned").exists())

    def test_failure_does_not_write_completion_marker_and_retry_reuses_account(self):
        with tempfile.TemporaryDirectory() as d:
            backend = FakeBackend(d, fail_on="ssh-keygen")
            with self.assertRaises(subprocess.CalledProcessError):
                provision(ProvisionConfig("m17s-box"), backend=backend, password="a" * 12)
            self.assertFalse(Path(d, "etc/m17s-armbian/provisioned").exists())
            backend.fail_on = None
            provision(ProvisionConfig("m17s-box"), backend=backend, password="a" * 12)
            self.assertTrue(Path(d, "etc/m17s-armbian/provisioned").exists())

    def test_existing_unmanaged_account_is_rejected_before_hostname_change(self):
        with tempfile.TemporaryDirectory() as d:
            backend = FakeBackend(d, preexisting=True)
            with self.assertRaises(RuntimeError):
                provision(ProvisionConfig("m17s-box"), backend=backend, password="a" * 12)
            self.assertEqual(backend.commands, [])


class FakeBackend:
    def __init__(self, root, fail_on=None, preexisting=False):
        self.root = Path(root)
        self.fail_on = fail_on
        self.preexisting = preexisting
        self.commands = []
        self.user = type("User", (), {"pw_dir": str(self.root / "home/m17s"), "pw_uid": os.getuid(), "pw_gid": os.getgid()})()

    def run(self, command, **kwargs):
        command = tuple(command)
        self.commands.append(command)
        if self.fail_on and self.fail_on in command:
            raise subprocess.CalledProcessError(1, command)

    def account(self, username):
        return self.user if self.preexisting or (self.root / "etc/m17s-armbian/account-created").exists() else None

    def write_atomic(self, path, data, mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
        os.chmod(path, mode)

    def mark_account_created(self, username):
        self.write_atomic(self.root / "etc/m17s-armbian/account-created", username + "\n")

    def account_was_created_by_service(self, username):
        return (self.root / "etc/m17s-armbian/account-created").read_text().strip() == username

    def ensure_account(self, username):
        user = self.account(username)
        if user is not None and not (self.root / "etc/m17s-armbian/account-created").exists():
            raise RuntimeError("refusing to take over existing account m17s")
        if user is None:
            self.run(["useradd", "--create-home", "--groups", "sudo", "--shell", "/bin/bash", username])
            self.mark_account_created(username)
            user = self.user
        return user

    def write_marker(self):
        self.write_atomic(self.root / "etc/m17s-armbian/provisioned", "provisioned\n")


if __name__ == "__main__":
    unittest.main()
