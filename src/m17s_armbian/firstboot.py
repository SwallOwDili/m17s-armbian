#!/usr/bin/env python3
"""First boot provisioning for the generic M17S Armbian image.

The JSON file deliberately has no password field.  A missing file selects the
interactive tty1 flow; a key-only JSON account receives passwordless sudo.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import json
import os
import pwd
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SUPPORTED_KEY_TYPES = {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"}
CONFIG_KEYS = {"hostname", "username", "ssh_authorized_keys"}


@dataclass(frozen=True)
class ProvisionConfig:
    hostname: str
    username: str = "m17s"
    ssh_authorized_keys: tuple[str, ...] = ()


def _validate_hostname(value: object) -> str:
    if not isinstance(value, str) or len(value) > 63 or not HOSTNAME_RE.fullmatch(value):
        raise ValueError("hostname must be a DNS label of 1-63 letters, digits, or hyphens")
    return value


def _validate_username(value: object) -> str:
    if not isinstance(value, str) or not USERNAME_RE.fullmatch(value) or value in {"root", "nobody"}:
        raise ValueError("username is invalid or reserved")
    return value


def _ssh_string(blob: bytes, offset: int = 0) -> tuple[bytes, int]:
    if len(blob) < offset + 4:
        raise ValueError("SSH key blob is truncated")
    size = int.from_bytes(blob[offset : offset + 4], "big")
    end = offset + 4 + size
    if size == 0 or end > len(blob):
        raise ValueError("SSH key blob is malformed")
    return blob[offset + 4 : end], end


def validate_authorized_key(value: object) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError("SSH key must be one physical line")
    fields = value.split(None, 2)
    if len(fields) < 2 or fields[0] not in SUPPORTED_KEY_TYPES:
        raise ValueError("SSH key type is unsupported or authorized_keys options are present")
    key_type, encoded = fields[0], fields[1]
    if encoded.startswith("-----BEGIN") or encoded.startswith("ssh-"):
        raise ValueError("private keys and nested key text are not accepted")
    try:
        blob = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("SSH key is not valid base64") from exc
    actual, end = _ssh_string(blob)
    if actual.decode("ascii", "strict") != key_type or end > len(blob):
        raise ValueError("SSH key blob type does not match its prefix")
    if key_type == "ssh-ed25519":
        key, end = _ssh_string(blob, end)
        if len(key) != 32 or end != len(blob):
            raise ValueError("invalid Ed25519 public key")
    elif key_type == "ecdsa-sha2-nistp256":
        curve, end = _ssh_string(blob, end)
        point, end = _ssh_string(blob, end)
        if curve != b"nistp256" or not point or point[:1] != b"\x04" or end != len(blob):
            raise ValueError("invalid ECDSA P-256 public key")
    else:  # ssh-rsa: exponent and modulus are mpints; reject trailing garbage.
        _, end = _ssh_string(blob, end)
        _, end = _ssh_string(blob, end)
        if end != len(blob):
            raise ValueError("invalid RSA public key")
    return value


def parse_config(path: os.PathLike[str] | str = "/boot/firstboot.json") -> ProvisionConfig | None:
    """Parse and validate firstboot.json without performing any system action."""
    config_path = Path(path)
    if not config_path.exists():
        return None
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid firstboot.json") from exc
    if not isinstance(raw, dict) or set(raw) - CONFIG_KEYS:
        raise ValueError("config must be an object with only hostname, username, and ssh_authorized_keys")
    if "hostname" not in raw:
        raise ValueError("hostname is required")
    hostname = _validate_hostname(raw["hostname"])
    username = _validate_username(raw.get("username", "m17s"))
    keys = raw.get("ssh_authorized_keys", [])
    if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys):
        raise ValueError("ssh_authorized_keys must be a list of strings")
    validated = tuple(validate_authorized_key(k) for k in keys)
    return ProvisionConfig(hostname, username, validated)


def interactive_config(input_fn: Callable[[str], str] = input,
                       password_fn: Callable[[str], str] = getpass.getpass) -> ProvisionConfig:
    """Collect the non-secret settings on tty1; passwords are never in config."""
    hostname = _validate_hostname(input_fn("Hostname: ").strip())
    username = _validate_username(input_fn("Username [m17s]: ").strip() or "m17s")
    return ProvisionConfig(hostname, username, ())


def interactive_password(password_fn: Callable[[str], str] = getpass.getpass) -> str:
    """Read and confirm a password for a config without SSH keys."""
    first = password_fn("Password (minimum 12 characters): ")
    second = password_fn("Repeat password: ")
    _validate_password(first)
    if first != second:
        raise ValueError("passwords must match and be at least 12 characters")
    return first


def _validate_password(value: object) -> str:
    if not isinstance(value, str) or len(value) < 12 or any(c in value for c in "\r\n:"):
        raise ValueError("password must be at least 12 characters and contain no CR, LF, or colon")
    return value


class SystemBackend:
    """The only production backend; it always operates on the running image."""

    root = Path("/")

    def run(self, command: Sequence[str], **kwargs: object) -> object:
        return subprocess.run(list(command), check=True, **kwargs)

    def account(self, username: str):
        try:
            return pwd.getpwnam(username)
        except KeyError:
            return None

    def write_atomic(self, path: Path, data: str, mode: int = 0o644) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        # Persist the directory entry as well as the file contents. This keeps
        # the completion marker recoverable across a sudden power loss.
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def mark_account_created(self, username: str) -> None:
        self.write_atomic(self.root / "etc/m17s-armbian/account-created", username + "\n")

    def account_was_created_by_service(self, username: str) -> bool:
        marker = self.root / "etc/m17s-armbian/account-created"
        return marker.exists() and marker.read_text(encoding="utf-8").strip() == username

    def ensure_account(self, username: str):
        user = self.account(username)
        if user is None:
            self.run(["useradd", "--create-home", "--groups", "sudo", "--shell", "/bin/bash", username])
            self.mark_account_created(username)
            user = self.account(username)
        elif not self.account_was_created_by_service(username):
            raise RuntimeError(f"refusing to take over existing account {username}")
        if user is None:
            raise RuntimeError("useradd succeeded but account lookup failed")
        return user

    def write_marker(self) -> None:
        self.write_atomic(self.root / "etc/m17s-armbian/provisioned", "provisioned\n")


def provision(config: ProvisionConfig, *, backend: SystemBackend | None = None,
              password: str | None = None) -> None:
    """Apply a previously validated config. All validation happens first."""
    config = ProvisionConfig(_validate_hostname(config.hostname), _validate_username(config.username),
                             tuple(validate_authorized_key(k) for k in config.ssh_authorized_keys))
    if password is not None:
        password = _validate_password(password)
    if not config.ssh_authorized_keys and password is None:
        raise ValueError("interactive provisioning requires a password of at least 12 characters")
    backend = backend or SystemBackend()
    user = backend.ensure_account(config.username)
    backend.run(["hostnamectl", "set-hostname", config.hostname])
    if password is not None:
        backend.run(["chpasswd"], input=f"{config.username}:{password}\n", text=True)
    else:
        sudoers = backend.root / f"etc/sudoers.d/{config.username}"
        backend.write_atomic(sudoers, f"{config.username} ALL=(ALL) NOPASSWD: ALL\n", 0o440)
        os.chmod(sudoers, 0o440)
    backend.run(["passwd", "-l", "root"])
    backend.run(["ssh-keygen", "-A"])
    ssh_dir = Path(user.pw_dir) / ".ssh"
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ssh_dir, 0o700)
    os.chown(ssh_dir, user.pw_uid, user.pw_gid)
    if config.ssh_authorized_keys:
        key_file = ssh_dir / "authorized_keys"
        backend.write_atomic(key_file, "".join(k + "\n" for k in config.ssh_authorized_keys), 0o600)
        os.chmod(key_file, 0o600)
        os.chown(key_file, user.pw_uid, user.pw_gid)
    backend.write_marker()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="apply provisioning to this image")
    args = parser.parse_args(argv)
    image_marker = Path("/etc/m17s-armbian/image")
    marker = Path("/etc/m17s-armbian/provisioned")
    if not args.apply:
        parser.error("--apply is required")
    if os.geteuid() != 0:
        parser.error("must run as root")
    if not image_marker.exists():
        parser.error("refusing to run without /etc/m17s-armbian/image")
    if marker.exists():
        return 0
    config = parse_config()
    password = None
    if config is None:
        config = interactive_config()
        password = interactive_password()
    elif not config.ssh_authorized_keys:
        password = interactive_password()
    provision(config, password=password)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
