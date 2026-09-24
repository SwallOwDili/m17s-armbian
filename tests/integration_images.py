#!/usr/bin/env python3
"""Read-only Linux acceptance checks for built M17S release images.

This verifier never writes an image.  It verifies the release hashes, expands
the three gzip artifacts into a private temporary directory, attaches bounded
read-only loop devices, and mounts every filesystem read-only.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from m17s_armbian import build


EMMC_BOOT_UUID = "2026-EA01"
USB_BOOT_UUID = "2026-AB01"
BOOT_OFFSET = 8192 * 512
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def run(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), check=True, **kwargs)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def regular(path: Path) -> Path:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"missing release file: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"release input must be a regular non-symlink file: {path}")
    return path


def parse_checksums(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for number, line in enumerate(regular(path).read_text(encoding="ascii").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)", line)
        if match is None:
            raise ValueError(f"invalid SHA256SUMS line {number}")
        digest, name = match.groups()
        if name in result:
            raise ValueError(f"duplicate SHA256SUMS entry: {name}")
        result[name] = digest
    return result


def load_release(dist: Path) -> tuple[dict, dict[str, str]]:
    checksums = parse_checksums(dist / "SHA256SUMS")
    release_path = regular(dist / "release.json")
    if checksums.get("release.json") != sha256(release_path):
        raise ValueError("release.json does not match SHA256SUMS")
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("release.json is invalid") from exc
    if not isinstance(release, dict) or release.get("schema_version") != 1:
        raise ValueError("unsupported release manifest")
    artifacts = release.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"emmc_root", "emmc_boot", "usb"}:
        raise ValueError("release manifest must contain exactly three image artifacts")
    for logical_name, entry in artifacts.items():
        if not isinstance(entry, dict):
            raise ValueError(f"invalid artifact entry: {logical_name}")
        filename = entry.get("file")
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".gz"):
            raise ValueError(f"unsafe artifact filename: {filename!r}")
        if checksums.get(filename) != entry.get("sha256"):
            raise ValueError(f"SHA256SUMS disagrees with release.json for {filename}")
        if not isinstance(entry.get("size"), int) or entry["size"] <= 0:
            raise ValueError(f"invalid compressed size for {filename}")
        if not isinstance(entry.get("raw_size"), int) or entry["raw_size"] <= 0:
            raise ValueError(f"invalid raw size for {filename}")
        if not isinstance(entry.get("raw_sha256"), str) or _SHA256.fullmatch(entry["raw_sha256"]) is None:
            raise ValueError(f"invalid raw SHA-256 for {filename}")
    return release, checksums


def expand_artifact(dist: Path, entry: dict, destination: Path) -> dict[str, object]:
    compressed = regular(dist / entry["file"])
    if compressed.stat().st_size != entry["size"] or sha256(compressed) != entry["sha256"]:
        raise ValueError(f"compressed artifact hash or size mismatch: {compressed.name}")
    digest = hashlib.sha256()
    size = 0
    chunk_size = 4 * 1024**2
    with gzip.open(compressed, "rb") as source, destination.open("xb") as output:
        while block := source.read(chunk_size):
            digest.update(block)
            size += len(block)
            if len(block) == chunk_size and block.count(0) == chunk_size:
                output.seek(chunk_size, os.SEEK_CUR)
            else:
                output.write(block)
        output.truncate(size)
    if size != entry["raw_size"] or digest.hexdigest() != entry["raw_sha256"]:
        raise ValueError(f"expanded artifact hash or size mismatch: {compressed.name}")
    allocated_size = destination.stat().st_blocks * 512
    return {
        "compressed_file": compressed.name,
        "compressed_sha256": entry["sha256"],
        "raw_sha256": entry["raw_sha256"],
        "raw_size": size,
        "allocated_size": allocated_size,
        "sparse_bytes_saved": max(0, size - allocated_size),
    }


@contextlib.contextmanager
def readonly_filesystem(
    image: Path, mountpoint: Path, *, offset: int = 0, size: int = 0, ext4: bool = False
):
    command = ["losetup", "--find", "--show", "--read-only", "--offset", str(offset)]
    if size:
        command += ["--sizelimit", str(size)]
    command.append(str(regular(image)))
    device = run(*command, capture_output=True, text=True).stdout.strip()
    mounted = False
    try:
        mountpoint.mkdir(mode=0o700)
        options = "ro,noload" if ext4 else "ro"
        run("mount", "-o", options, device, str(mountpoint))
        mounted = True
        yield device, mountpoint
    finally:
        if mounted:
            run("umount", str(mountpoint))
        run("losetup", "-d", device)


def blkid_value(device: str, field: str) -> str:
    return run(
        "blkid", "-p", "-s", field, "-o", "value", device,
        capture_output=True, text=True,
    ).stdout.strip()


def require_regular(root: Path, relative: str) -> Path:
    path = root / relative
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"required image file is missing: /{relative}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"required image path is not a regular file: /{relative}")
    return path


def require_absent(root: Path, relative: str) -> None:
    path = root / relative
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise ValueError(f"forbidden image path exists: /{relative}")


def privacy_inventory(root: Path) -> None:
    patterns = (
        "etc/ssh/ssh_host_*",
        "root/.ssh/*",
        "home/*/.ssh/*",
        "**/authorized_keys",
        "**/id_rsa",
        "**/id_ed25519",
        "root/.*history",
        "home/*/.*history",
        "etc/NetworkManager/system-connections/*",
        "var/lib/dhcp/*",
        "var/lib/NetworkManager/*",
        "var/lib/systemd/random-seed",
    )
    forbidden = sorted(
        str(path.relative_to(root))
        for pattern in patterns
        for path in root.glob(pattern)
    )
    if forbidden:
        raise ValueError("privacy residue in root filesystem: " + ", ".join(forbidden))
    if require_regular(root, "etc/machine-id").read_bytes():
        raise ValueError("/etc/machine-id is not empty")
    require_absent(root, "etc/m17s-armbian/provisioned")
    shadow = require_regular(root, "etc/shadow").read_text(encoding="utf-8")
    root_lines = [line for line in shadow.splitlines() if line.startswith("root:")]
    if len(root_lines) != 1 or root_lines[0].split(":", 2)[1] not in {"!", "*", "!!"}:
        raise ValueError("root account is not locked")


def verify_login_gates(root: Path) -> None:
    systemd = root / "etc/systemd/system"
    for old_dropin in ("getty@.service.d", "serial-getty@.service.d"):
        require_absent(root, f"etc/systemd/system/{old_dropin}")
    for path in systemd.rglob("*.conf"):
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"systemd drop-in is not a regular file: {path.relative_to(root)}")
        if b"--autologin" in path.read_bytes():
            raise ValueError(f"automatic login remains configured: {path.relative_to(root)}")
    expected_gate = (
        "[Unit]\n"
        "After=m17s-firstboot.service\n"
        "ConditionPathExists=/etc/m17s-armbian/provisioned\n"
    )
    for unit in ("ssh.service", "ssh.socket"):
        gate = require_regular(root, f"etc/systemd/system/{unit}.d/m17s-firstboot.conf")
        if gate.read_text(encoding="utf-8") != expected_gate:
            raise ValueError(f"{unit} does not have the exact firstboot marker gate")
    firstboot = require_regular(root, "etc/systemd/system/m17s-firstboot.service").read_text(
        encoding="utf-8"
    )
    if "Before=ssh.service ssh.socket getty@tty1.service" not in firstboot:
        raise ValueError("firstboot service is not ordered before ssh.service and ssh.socket")


def verify_console_setup_ordering(root: Path) -> str:
    expected = "[Unit]\nAfter=systemd-tmpfiles-setup.service\n"
    dropin = require_regular(
        root, "etc/systemd/system/console-setup.service.d/m17s-tmpfiles.conf"
    )
    if dropin.read_text(encoding="utf-8") != expected:
        raise ValueError("console-setup tmpfiles ordering drop-in has unexpected content")
    # The ordering fix must augment the packaged unit, never replace or mask it.
    require_absent(root, "etc/systemd/system/console-setup.service")
    require_regular(root, "lib/systemd/system/console-setup.service")
    return "ok"


def verify_source_files(root: Path, source_files: object) -> dict[str, dict[str, str]]:
    if not isinstance(source_files, dict):
        raise ValueError("release source_files manifest is missing")
    package_entries = {
        name: digest
        for name, digest in source_files.items()
        if isinstance(name, str) and name.startswith("src/m17s_armbian/") and name.endswith(".py")
    }
    if not package_entries:
        raise ValueError("release source_files contains no installed Python package")
    package_root = root / "usr/local/lib/m17s-armbian/m17s_armbian"
    expected_relative = {
        str(Path(source).relative_to("src/m17s_armbian")): source
        for source in package_entries
    }
    actual_relative = {
        str(path.relative_to(package_root))
        for path in package_root.glob("*.py")
        if stat.S_ISREG(path.lstat().st_mode)
    }
    if actual_relative != set(expected_relative):
        raise ValueError(
            "installed Python package file set differs from release source_files: "
            f"expected={sorted(expected_relative)} actual={sorted(actual_relative)}"
        )
    mapping: dict[str, dict[str, str]] = {}
    for relative, source in sorted(expected_relative.items()):
        expected_hash = package_entries[source]
        if not isinstance(expected_hash, str) or _SHA256.fullmatch(expected_hash) is None:
            raise ValueError(f"invalid source_files hash for {source}")
        installed = require_regular(package_root, relative)
        actual_hash = sha256(installed)
        if actual_hash != expected_hash:
            raise ValueError(f"installed package hash differs from source snapshot: {source}")
        mapping[source] = {
            "image_path": "/usr/local/lib/m17s-armbian/m17s_armbian/" + relative,
            "sha256": actual_hash,
        }
    return mapping


def verify_root(
    root: Path, *, root_uuid: str, boot_uuid: str, media: str, source_files: object
) -> dict[str, object]:
    expected_fstab = build.fstab(root_uuid, boot_uuid)
    actual_fstab = require_regular(root, "etc/fstab").read_text(encoding="utf-8")
    if actual_fstab != expected_fstab:
        raise ValueError(f"{media} root fstab does not match its filesystem UUID contract")
    privacy_inventory(root)
    verify_login_gates(root)
    console_setup_ordering = verify_console_setup_ordering(root)
    if not require_regular(root, "etc/m17s-armbian/image").read_text().strip():
        raise ValueError("image marker is empty")
    require_regular(root, "etc/systemd/system/m17s-firstboot.service")
    enabled = root / "etc/systemd/system/multi-user.target.wants/m17s-firstboot.service"
    if not enabled.is_symlink() or os.readlink(enabled) != "/etc/systemd/system/m17s-firstboot.service":
        raise ValueError("m17s-firstboot.service is not enabled")
    for command in ("m17s-emmc", "m17s-boot", "m17s-firstboot"):
        require_regular(root, f"usr/local/sbin/{command}")
    require_regular(root, "usr/local/lib/m17s-armbian/m17s_armbian/installer.py")
    if media == "usb":
        if require_regular(root, "etc/m17s-armbian/media").read_text().strip() != "usb":
            raise ValueError("USB media marker is wrong")
    else:
        require_absent(root, "etc/m17s-armbian/media")
    return {
        "fstab": "ok",
        "privacy": "ok",
        "firstboot": "enabled",
        "console_setup_ordering": console_setup_ordering,
        "source_files": verify_source_files(root, source_files),
    }


def verify_boot(boot_root: Path, *, root_uuid: str, usb: bool) -> dict[str, object]:
    expected_uenv = build.uenv(root_uuid)
    if require_regular(boot_root, "uEnv.txt").read_text(encoding="utf-8") != expected_uenv:
        raise ValueError("boot uEnv.txt does not match its root UUID contract")
    for relative in ("zImage", "uInitrd", build.DTB, "u-boot-p212.bin"):
        require_regular(boot_root, relative)
    require_absent(boot_root, "emmc_autoscript")
    if usb:
        for relative in ("boot.cmd", "boot.scr", "s905_autoscript", "s905_autoscript.cmd", "u-boot.ext"):
            require_regular(boot_root, relative)
        boot_script = require_regular(boot_root, "boot.scr").read_bytes()
        if b"env import -t" not in boot_script or b"booti " not in boot_script:
            raise ValueError("upstream boot.scr does not contain the expected uEnv/booti path")
        vendor_script = require_regular(boot_root, "s905_autoscript").read_bytes()
        if b"u-boot.ext" not in vendor_script or b"go 0x1000000" not in vendor_script:
            raise ValueError("s905_autoscript does not contain the expected u-boot.ext chain")
        if sha256(boot_root / "u-boot.ext") != sha256(boot_root / "u-boot-p212.bin"):
            raise ValueError("USB u-boot.ext is not the pinned P212 U-Boot")
        require_regular(boot_root, "multiboot-activation/aml_autoscript")
        require_regular(boot_root, "FIRST-BOOT.txt")
        require_absent(boot_root, "M17S-UNACTIVATED.txt")
    else:
        require_regular(boot_root, "M17S-UNACTIVATED.txt")
        require_absent(boot_root, "u-boot.ext")
    return {"uenv": "ok", "activation_absent": True, "usb_chain": "ok" if usb else None}


def verify_mbr(image: Path) -> dict[str, object]:
    with image.open("rb") as stream:
        sector = stream.read(512)
    if len(sector) != 512 or sector[510:512] != b"\x55\xaa":
        raise ValueError("USB image has no valid DOS MBR signature")
    entries = []
    for index in range(2):
        _status, kind, start, count = struct.unpack_from("<B3xB3xII", sector, 446 + 16 * index)
        entries.append({"type": kind, "start": start, "sectors": count})
    expected = [
        {"type": 0x0C, "start": 8192, "sectors": build.BOOT_BYTES // 512},
        {"type": 0x83, "start": build.USB_ROOT_SECTOR, "sectors": build.ROOT_BYTES // 512},
    ]
    if entries != expected:
        raise ValueError(f"USB MBR partition contract mismatch: {entries!r}")
    expected_size = (build.USB_ROOT_SECTOR * 512) + build.ROOT_BYTES
    if image.stat().st_size != expected_size:
        raise ValueError("USB raw image size does not match its partition table")
    return {"signature": "55aa", "partitions": entries}


def verify_release(dist: Path, temporary_parent: Path | None = None) -> dict[str, object]:
    dist = dist.resolve()
    release, _checksums = load_release(dist)
    with tempfile.TemporaryDirectory(prefix="m17s-image-verify-", dir=temporary_parent) as name:
        temporary = Path(name)
        os.chmod(temporary, 0o700)
        raw_paths = {
            logical: temporary / f"{logical}.img"
            for logical in ("emmc_root", "emmc_boot", "usb")
        }
        artifact_results = {
            logical: expand_artifact(dist, release["artifacts"][logical], raw_paths[logical])
            for logical in ("emmc_root", "emmc_boot", "usb")
        }
        mounts = {key: temporary / f"mnt-{key}" for key in ("eroot", "eboot", "uroot", "uboot")}
        with readonly_filesystem(raw_paths["emmc_root"], mounts["eroot"], ext4=True) as (erootdev, eroot), \
             readonly_filesystem(raw_paths["emmc_boot"], mounts["eboot"]) as (ebootdev, eboot), \
             readonly_filesystem(
                 raw_paths["usb"], mounts["uroot"],
                 offset=build.USB_ROOT_SECTOR * 512, size=build.ROOT_BYTES, ext4=True,
             ) as (urootdev, uroot), \
             readonly_filesystem(
                 raw_paths["usb"], mounts["uboot"], offset=BOOT_OFFSET, size=build.BOOT_BYTES,
             ) as (ubootdev, uboot):
            identifiers = {
                "emmc_root": blkid_value(erootdev, "UUID"),
                "emmc_boot": blkid_value(ebootdev, "UUID"),
                "usb_root": blkid_value(urootdev, "UUID"),
                "usb_boot": blkid_value(ubootdev, "UUID"),
            }
            expected_identifiers = {
                "emmc_root": build.EMMC_UUID,
                "emmc_boot": EMMC_BOOT_UUID,
                "usb_root": build.USB_UUID,
                "usb_boot": USB_BOOT_UUID,
            }
            if identifiers != expected_identifiers:
                raise ValueError(f"filesystem UUID contract mismatch: {identifiers!r}")
            labels = {
                "emmc_root": blkid_value(erootdev, "LABEL"),
                "emmc_boot": blkid_value(ebootdev, "LABEL"),
                "usb_root": blkid_value(urootdev, "LABEL"),
                "usb_boot": blkid_value(ubootdev, "LABEL"),
            }
            expected_labels = {
                "emmc_root": "M17S_ROOT",
                "emmc_boot": "BOOT_EMMC",
                "usb_root": "M17S_ROOT",
                "usb_boot": "BOOT_USB",
            }
            if labels != expected_labels:
                raise ValueError(f"filesystem label contract mismatch: {labels!r}")
            filesystem_results = {
                "emmc_root": verify_root(
                    eroot, root_uuid=build.EMMC_UUID, boot_uuid=EMMC_BOOT_UUID, media="emmc",
                    source_files=release.get("source_files"),
                ),
                "emmc_boot": verify_boot(eboot, root_uuid=build.EMMC_UUID, usb=False),
                "usb_root": verify_root(
                    uroot, root_uuid=build.USB_UUID, boot_uuid=USB_BOOT_UUID, media="usb",
                    source_files=release.get("source_files"),
                ),
                "usb_boot": verify_boot(uboot, root_uuid=build.USB_UUID, usb=True),
            }
        emmc_sources = filesystem_results["emmc_root"].pop("source_files")
        usb_sources = filesystem_results["usb_root"].pop("source_files")
        if emmc_sources != usb_sources:
            raise ValueError("USB and eMMC root filesystems contain different installed source files")
        result = {
            "schema_version": 1,
            "status": "passed",
            "release_version": release.get("version"),
            "release_status": release.get("status"),
            "artifacts": artifact_results,
            "usb_mbr": verify_mbr(raw_paths["usb"]),
            "filesystem_identifiers": identifiers,
            "filesystem_labels": labels,
            "filesystems": filesystem_results,
            "source_files": emmc_sources,
            "read_only": True,
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True, help="release output directory")
    parser.add_argument("--tmp-dir", type=Path, help="parent for private expanded-image storage")
    parser.add_argument("--json-out", type=Path, help="optional result path; stdout is always emitted")
    args = parser.parse_args(argv)
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("verification requires root inside the Linux build container")
    if args.tmp_dir is not None:
        if args.tmp_dir.is_symlink():
            parser.error("--tmp-dir must be a real directory")
        regular_parent = args.tmp_dir.resolve()
        if not regular_parent.is_dir():
            parser.error("--tmp-dir must be a real directory")
    else:
        regular_parent = None
    try:
        result = verify_release(args.dist, regular_parent)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"integration_images.py: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_out is not None:
        output = args.json_out.resolve()
        if output.exists() or output.is_symlink():
            parser.error("--json-out refuses to overwrite an existing path")
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
