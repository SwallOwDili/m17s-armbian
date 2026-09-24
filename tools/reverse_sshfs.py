#!/usr/bin/env python3
"""Expose a Mac directory through an existing SSH connection.

The local OpenSSH SFTP server and the remote sshfs ``passive`` process are
connected by two anonymous pipes.  This script opens no listening socket and
never invokes a local shell.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import sys
import time


SFTP_SERVER = Path("/usr/libexec/sftp-server")
SSH = Path("/usr/bin/ssh")
_DESTINATION = re.compile(r"[A-Za-z0-9_.@:\[\]-]+\Z")


def _regular_file(value: str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be an existing regular non-symlink file")
    return path.resolve()


def _local_directory(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("--local-dir must be an absolute path")
    if path.is_symlink() or not path.is_dir():
        raise ValueError("--local-dir must be an existing non-symlink directory")
    return path.resolve()


def _remote_mount(value: str) -> str:
    if not value or any(character in value for character in "\0\r\n"):
        raise ValueError("--remote-mount contains an invalid character")
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or value == "/":
        raise ValueError("--remote-mount must be a normalized absolute path other than /")
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ValueError("--remote-mount contains an unsafe path component")
    return value


def _remote_executable(value: str) -> str:
    if not value or any(character in value for character in "\0\r\n"):
        raise ValueError("--remote-sshfs contains an invalid character")
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or value == "/":
        raise ValueError("--remote-sshfs must be a normalized absolute path other than /")
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ValueError("--remote-sshfs contains an unsafe path component")
    return value


def _hostuser(value: str) -> str:
    if (
        not value
        or value.startswith("-")
        or _DESTINATION.fullmatch(value) is None
        or value.count("@") > 1
    ):
        raise ValueError("--hostuser must be a single safe SSH destination such as user@host")
    return value


def _terminate(process: subprocess.Popen[bytes] | None, timeout: float = 5.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def run_bridge(
    *,
    hostuser: str,
    known_hosts: Path,
    local_dir: Path,
    remote_mount: str,
    remote_sshfs: str,
    read_write: bool,
    identity: Path | None,
    log_path: Path,
) -> int:
    mount_mode = "rw" if read_write else "ro"
    remote_arguments = [
        "sudo",
        "-n",
        "--",
        remote_sshfs,
        f":{local_dir}",
        remote_mount,
        "-o",
        f"passive,{mount_mode},uid=0,gid=0,default_permissions",
        "-f",
    ]
    remote_command = shlex.join(remote_arguments)
    ssh_arguments = [
        str(SSH),
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
    ]
    if identity is not None:
        ssh_arguments.extend(["-o", "IdentitiesOnly=yes", "-i", str(identity)])
    ssh_arguments.extend(["--", hostuser, remote_command])

    log_path.parent.mkdir(parents=True, exist_ok=True)
    stop_signal: int | None = None

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stop_signal
        stop_signal = signum

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    # local SFTP stdout -> SSH stdin
    ssh_input_read, sftp_output_write = os.pipe()
    # SSH stdout -> local SFTP stdin
    sftp_input_read, ssh_output_write = os.pipe()
    sftp: subprocess.Popen[bytes] | None = None
    ssh: subprocess.Popen[bytes] | None = None
    try:
        with log_path.open("ab", buffering=0) as log:
            log.write(
                (
                    f"\n--- reverse sshfs start pid={os.getpid()} destination={hostuser} "
                    f"local={local_dir} remote={remote_mount} ---\n"
                ).encode("utf-8")
            )
            try:
                sftp = subprocess.Popen(
                    [str(SFTP_SERVER)] + ([] if read_write else ["-R"]),
                    stdin=sftp_input_read,
                    stdout=sftp_output_write,
                    stderr=log,
                    close_fds=True,
                )
                ssh = subprocess.Popen(
                    ssh_arguments,
                    stdin=ssh_input_read,
                    stdout=ssh_output_write,
                    stderr=log,
                    close_fds=True,
                )
            finally:
                for descriptor in (
                    ssh_input_read,
                    sftp_output_write,
                    sftp_input_read,
                    ssh_output_write,
                ):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

            while stop_signal is None and sftp.poll() is None and ssh.poll() is None:
                time.sleep(0.2)

            # Closing the SSH transport normally makes passive sshfs exit and
            # release its own mount.  Only these exact child PIDs are signalled.
            _terminate(ssh)
            _terminate(sftp)
            ssh_status = ssh.returncode
            sftp_status = sftp.returncode
            log.write(
                f"--- reverse sshfs stop ssh={ssh_status} sftp={sftp_status} ---\n".encode(
                    "ascii"
                )
            )
            if stop_signal is not None:
                return 128 + stop_signal
            return 0 if ssh_status == 0 and sftp_status == 0 else 1
    finally:
        _terminate(ssh)
        _terminate(sftp)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostuser", required=True, help="existing SSH destination, e.g. user@box")
    parser.add_argument("--known-hosts", required=True, help="absolute pinned known_hosts file")
    parser.add_argument("--identity", help="optional absolute SSH private-key path")
    parser.add_argument("--local-dir", required=True, help="absolute Mac directory to expose")
    parser.add_argument("--remote-mount", required=True, help="existing absolute mountpoint on box")
    parser.add_argument(
        "--remote-sshfs",
        default="/usr/bin/sshfs",
        help="absolute sshfs executable on box (default: /usr/bin/sshfs)",
    )
    parser.add_argument(
        "--read-write",
        action="store_true",
        help="allow the box to modify the shared Mac directory (default: read-only)",
    )
    parser.add_argument(
        "--log",
        default=str(Path(__file__).with_name("reverse-sshfs.log")),
        help="child stderr log (default: beside this script)",
    )
    args = parser.parse_args(argv)
    try:
        hostuser = _hostuser(args.hostuser)
        known_hosts = _regular_file(args.known_hosts, "--known-hosts")
        identity = _regular_file(args.identity, "--identity") if args.identity else None
        local_dir = _local_directory(args.local_dir)
        remote_mount = _remote_mount(args.remote_mount)
        remote_sshfs = _remote_executable(args.remote_sshfs)
        log_path = Path(args.log).expanduser()
        if not log_path.is_absolute():
            raise ValueError("--log must be an absolute path")
        if log_path.is_symlink() or (log_path.exists() and not log_path.is_file()):
            raise ValueError("--log must be a regular non-symlink file")
        if not SFTP_SERVER.is_file() or not SSH.is_file():
            raise ValueError("required macOS /usr/libexec/sftp-server or /usr/bin/ssh is missing")
        return run_bridge(
            hostuser=hostuser,
            known_hosts=known_hosts,
            local_dir=local_dir,
            remote_mount=remote_mount,
            remote_sshfs=remote_sshfs,
            read_write=args.read_write,
            identity=identity,
            log_path=log_path,
        )
    except (OSError, ValueError) as exc:
        print(f"reverse_sshfs.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
