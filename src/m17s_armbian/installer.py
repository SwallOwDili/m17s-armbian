"""Conservative M17S eMMC installer.

The production path accepts only the single hardware and partition profile that
has been validated on the M17S.  Tests inject a low-level executor; the CLI has
no switch which permits regular files or loop devices.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import errno
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
from typing import Callable, Iterator, Mapping, Sequence

from . import boot, firstboot


SCHEMA_VERSION = 1
BOARD = "m17s-s905x-2g-emmc8g"
EMMC_PRODUCT = "8GME4R"
EMMC_BYTES = 7_818_182_656
SECTOR_SIZE = 512
PREFIX_BYTES = 700 * 1024 * 1024
BOOT_START_SECTOR = 700 * 2048
BOOT_SECTORS = 511 * 2048
ROOT_START_SECTOR = 1212 * 2048
BOOT_IMAGE_BYTES = 511 * 1024 * 1024
ROOT_IMAGE_BYTES = 4 * 1024 * 1024 * 1024
PROFILE_COMPATIBLE = "amlogic,p212"
PROFILE_RAM_BYTES = 2 * 1024 * 1024 * 1024
DTB_RELATIVE = Path("dtb/amlogic/meson-gxl-s905x-p212.dtb")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)


class InstallerError(RuntimeError):
    """A refusal or failed installer invariant."""


@dataclasses.dataclass(frozen=True)
class Partition:
    number: int
    path: str
    start_sector: int
    sectors: int
    mounts: tuple[str, ...] = ()
    holders: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "Partition":
        return cls(
            number=int(value["number"]),
            path=str(value["path"]),
            start_sector=int(value["start_sector"]),
            sectors=int(value["sectors"]),
            mounts=tuple(str(x) for x in value.get("mounts", [])),
            holders=tuple(str(x) for x in value.get("holders", [])),
        )


@dataclasses.dataclass(frozen=True)
class DeviceProbe:
    path: str
    major_minor: str
    sysfs_path: str
    cid: str
    product: str
    size_bytes: int
    sector_size: int
    removable: bool
    device_type: str
    compatible: tuple[str, ...]
    ram_bytes: int
    boot0: str
    boot1: str
    boot0_bytes: int
    boot1_bytes: int
    partitions: tuple[Partition, ...]
    root_disk: str
    mounts: tuple[str, ...] = ()
    swap_devices: tuple[str, ...] = ()
    holders: tuple[str, ...] = ()
    mbr_table_bytes: str = ""
    gpt_signature: bool = False

    def to_dict(self) -> dict[str, object]:
        value = dataclasses.asdict(self)
        value["partitions"] = [part.to_dict() for part in self.partitions]
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "DeviceProbe":
        return cls(
            path=str(value["path"]),
            major_minor=str(value["major_minor"]),
            sysfs_path=str(value["sysfs_path"]),
            cid=str(value["cid"]),
            product=str(value["product"]),
            size_bytes=int(value["size_bytes"]),
            sector_size=int(value["sector_size"]),
            removable=bool(value["removable"]),
            device_type=str(value["device_type"]),
            compatible=tuple(str(x) for x in value["compatible"]),
            ram_bytes=int(value["ram_bytes"]),
            boot0=str(value["boot0"]),
            boot1=str(value["boot1"]),
            boot0_bytes=int(value["boot0_bytes"]),
            boot1_bytes=int(value["boot1_bytes"]),
            partitions=tuple(Partition.from_dict(x) for x in value["partitions"]),
            root_disk=str(value["root_disk"]),
            mounts=tuple(str(x) for x in value.get("mounts", [])),
            swap_devices=tuple(str(x) for x in value.get("swap_devices", [])),
            holders=tuple(str(x) for x in value.get("holders", [])),
            mbr_table_bytes=str(value.get("mbr_table_bytes", "")),
            gpt_signature=bool(value.get("gpt_signature", False)),
        )


@dataclasses.dataclass(frozen=True)
class Artifact:
    name: str
    file: str
    sha256: str
    size: int
    raw_sha256: str
    raw_size: int

    @classmethod
    def from_dict(cls, name: str, value: Mapping[str, object]) -> "Artifact":
        required = {"file", "sha256", "size", "raw_sha256", "raw_size"}
        if set(value) != required:
            raise InstallerError(f"artifact {name} must contain exactly {sorted(required)}")
        artifact = cls(
            name=name,
            file=str(value["file"]),
            sha256=str(value["sha256"]),
            size=int(value["size"]),
            raw_sha256=str(value["raw_sha256"]),
            raw_size=int(value["raw_size"]),
        )
        if not SHA256_RE.fullmatch(artifact.sha256) or not SHA256_RE.fullmatch(artifact.raw_sha256):
            raise InstallerError(f"artifact {name} has an invalid SHA-256")
        if artifact.size <= 0 or artifact.raw_size <= 0:
            raise InstallerError(f"artifact {name} sizes must be positive")
        path = Path(artifact.file)
        if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
            raise InstallerError(f"artifact {name} file must be a release-directory basename")
        return artifact


@dataclasses.dataclass(frozen=True)
class Release:
    directory: str
    version: str
    manifest_sha256: str
    boot_artifact: Artifact
    root_artifact: Artifact


@dataclasses.dataclass(frozen=True)
class InstallPlan:
    schema_version: int
    board: str
    release_version: str
    release_manifest_sha256: str
    target: DeviceProbe
    initialize_layout: bool
    boot_artifact: Artifact
    root_artifact: Artifact

    def to_dict(self) -> dict[str, object]:
        def artifact_value(item: Artifact) -> dict[str, object]:
            return {key: value for key, value in dataclasses.asdict(item).items() if key != "name"}
        return {
            "schema_version": self.schema_version,
            "board": self.board,
            "release_version": self.release_version,
            "release_manifest_sha256": self.release_manifest_sha256,
            "target": self.target.to_dict(),
            "initialize_layout": self.initialize_layout,
            "artifacts": {
                "emmc_boot": artifact_value(self.boot_artifact),
                "emmc_root": artifact_value(self.root_artifact),
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "InstallPlan":
        artifacts = value["artifacts"]
        if not isinstance(artifacts, Mapping):
            raise InstallerError("plan artifacts must be an object")
        def artifact(name: str) -> Artifact:
            raw = dict(artifacts[name])
            raw.pop("name", None)
            return Artifact.from_dict(name, raw)
        return cls(
            schema_version=int(value["schema_version"]),
            board=str(value["board"]),
            release_version=str(value["release_version"]),
            release_manifest_sha256=str(value["release_manifest_sha256"]),
            target=DeviceProbe.from_dict(value["target"]),
            initialize_layout=bool(value["initialize_layout"]),
            boot_artifact=artifact("emmc_boot"),
            root_artifact=artifact("emmc_root"),
        )


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.EBADF, errno.ENOTSUP}:
            raise


def load_release(directory: os.PathLike[str] | str) -> Release:
    release_dir = Path(directory).resolve()
    manifest_path = release_dir / "release.json"
    try:
        encoded = manifest_path.read_bytes()
        raw = json.loads(encoded)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError(f"cannot read release manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise InstallerError("release manifest must be a JSON object")
    required = {"schema_version", "version", "board", "artifacts"}
    optional = {
        "kernel_release",
        "privacy",
        "reproducibility",
        "signature",
        "source_files",
        "sources",
        "status",
    }
    missing = required - set(raw)
    unknown = set(raw) - required - optional
    if missing:
        raise InstallerError(f"release manifest is missing fields: {sorted(missing)}")
    if unknown:
        raise InstallerError(f"release manifest has unknown fields: {sorted(unknown)}")
    if raw["schema_version"] != SCHEMA_VERSION or raw["board"] != BOARD:
        raise InstallerError("release schema or board profile is unsupported")
    if not isinstance(raw["version"], str) or not raw["version"]:
        raise InstallerError("release version must be a non-empty string")
    artifacts = raw["artifacts"]
    if not isinstance(artifacts, dict):
        raise InstallerError("release artifacts must be a JSON object")
    required_artifacts = {"emmc_boot", "emmc_root"}
    missing_artifacts = required_artifacts - set(artifacts)
    unknown_artifacts = set(artifacts) - required_artifacts - {"usb"}
    if missing_artifacts:
        raise InstallerError(f"release is missing artifacts: {sorted(missing_artifacts)}")
    if unknown_artifacts:
        raise InstallerError(f"release has unknown artifacts: {sorted(unknown_artifacts)}")
    boot_artifact = Artifact.from_dict("emmc_boot", artifacts["emmc_boot"])
    root_artifact = Artifact.from_dict("emmc_root", artifacts["emmc_root"])
    if "usb" in artifacts:
        # Validate the published metadata schema, but an eMMC-only installer
        # bundle does not need to carry or verify the unrelated USB image.
        Artifact.from_dict("usb", artifacts["usb"])
    if boot_artifact.raw_size != BOOT_IMAGE_BYTES:
        raise InstallerError(f"emmc_boot raw_size must be {BOOT_IMAGE_BYTES}")
    if root_artifact.raw_size != ROOT_IMAGE_BYTES:
        raise InstallerError(f"emmc_root raw_size must be {ROOT_IMAGE_BYTES}")
    for artifact in (boot_artifact, root_artifact):
        path = release_dir / artifact.file
        if not path.is_file():
            raise InstallerError(f"release artifact is missing: {artifact.file}")
        digest, size = _sha256_file(path)
        if size != artifact.size or digest != artifact.sha256:
            raise InstallerError(f"compressed artifact verification failed: {artifact.file}")
    return Release(
        directory=str(release_dir),
        version=raw["version"],
        manifest_sha256=hashlib.sha256(encoded).hexdigest(),
        boot_artifact=boot_artifact,
        root_artifact=root_artifact,
    )


def _validate_common_probe(probe: DeviceProbe) -> None:
    errors: list[str] = []
    if probe.device_type != "MMC" or probe.removable:
        errors.append("target is not a non-removable MMC device")
    if probe.product != EMMC_PRODUCT:
        errors.append(f"MMC product is not {EMMC_PRODUCT}")
    if probe.size_bytes != EMMC_BYTES or probe.sector_size != SECTOR_SIZE:
        errors.append("MMC capacity or logical sector size does not match the validated profile")
    if PROFILE_COMPATIBLE not in probe.compatible or probe.ram_bytes != PROFILE_RAM_BYTES:
        errors.append("device tree is not the validated P212 2 GiB profile")
    if not probe.boot0 or not probe.boot1 or probe.boot0_bytes <= 0 or probe.boot1_bytes <= 0:
        errors.append("MMC boot0/boot1 devices are missing")
    if probe.path == probe.root_disk:
        errors.append("target is the running root/USB source disk")
    if probe.swap_devices:
        errors.append("target or a target partition is active swap")
    if probe.holders or any(part.holders for part in probe.partitions):
        errors.append("target has device-mapper/holder users")
    if probe.mounts:
        errors.append("the whole target device is mounted")
    if any(part.mounts for part in probe.partitions):
        errors.append("a target partition is mounted")
    if errors:
        raise InstallerError("; ".join(errors))


def _validate_layout(probe: DeviceProbe, initialize_layout: bool) -> None:
    _validate_common_probe(probe)
    if initialize_layout:
        if probe.partitions:
            raise InstallerError("--initialize-layout requires a target with no existing partitions")
        if probe.mbr_table_bytes != (b"\0" * 66).hex():
            raise InstallerError("fresh-layout target MBR bytes 446..511 are not all zero")
        if probe.gpt_signature:
            raise InstallerError("fresh-layout target contains a GPT signature")
        return
    if probe.gpt_signature:
        raise InstallerError("existing-layout target contains a GPT signature")
    if len(probe.partitions) != 2:
        raise InstallerError("existing install requires exactly two partitions")
    parts = {part.number: part for part in probe.partitions}
    if set(parts) != {1, 2}:
        raise InstallerError("existing install requires partition numbers 1 and 2 only")
    p1, p2 = parts[1], parts[2]
    if p1.start_sector != BOOT_START_SECTOR or p1.sectors != BOOT_SECTORS:
        raise InstallerError("partition 1 does not match the validated 700 MiB/511 MiB layout")
    if p2.start_sector != ROOT_START_SECTOR:
        raise InstallerError("partition 2 does not start at the validated 1212 MiB offset")
    total_sectors = probe.size_bytes // probe.sector_size
    if p2.sectors != total_sectors - ROOT_START_SECTOR:
        raise InstallerError("partition 2 does not extend to the final device sector")
    try:
        table = bytes.fromhex(probe.mbr_table_bytes)
    except ValueError as exc:
        raise InstallerError("existing-layout target has malformed MBR bytes") from exc
    if len(table) != 66 or table[64:66] != b"\x55\xaa":
        raise InstallerError("existing-layout target lacks the MBR 55aa signature")
    expected = (
        (0x0C, p1.start_sector, p1.sectors),
        (0x83, p2.start_sector, p2.sectors),
    )
    for index, (part_type, start, sectors) in enumerate(expected):
        entry = table[index * 16 : (index + 1) * 16]
        if entry[0] not in {0x00, 0x80}:
            raise InstallerError(f"MBR partition {index + 1} has an invalid boot flag")
        actual = (entry[4], int.from_bytes(entry[8:12], "little"),
                  int.from_bytes(entry[12:16], "little"))
        if actual != (part_type, start, sectors):
            raise InstallerError(
                f"MBR partition {index + 1} type/LBA geometry does not match sysfs"
            )
    if table[32:64] != b"\0" * 32:
        raise InstallerError("MBR contains partition entries other than p1 and p2")


def create_plan(release: Release, probe: DeviceProbe, *, initialize_layout: bool) -> InstallPlan:
    _validate_layout(probe, initialize_layout)
    return InstallPlan(
        schema_version=SCHEMA_VERSION,
        board=BOARD,
        release_version=release.version,
        release_manifest_sha256=release.manifest_sha256,
        target=probe,
        initialize_layout=initialize_layout,
        boot_artifact=release.boot_artifact,
        root_artifact=release.root_artifact,
    )


def _probe_identity(probe: DeviceProbe, *, include_layout: bool = True) -> dict[str, object]:
    result: dict[str, object] = {
        "path": probe.path,
        "major_minor": probe.major_minor,
        "sysfs_path": probe.sysfs_path,
        "cid": probe.cid,
        "product": probe.product,
        "size_bytes": probe.size_bytes,
        "sector_size": probe.sector_size,
        "boot0": probe.boot0,
        "boot1": probe.boot1,
        "boot0_bytes": probe.boot0_bytes,
        "boot1_bytes": probe.boot1_bytes,
        "removable": probe.removable,
        "device_type": probe.device_type,
        "compatible": list(probe.compatible),
        "ram_bytes": probe.ram_bytes,
        "root_disk": probe.root_disk,
        "mounts": list(probe.mounts),
        "swap_devices": list(probe.swap_devices),
        "holders": list(probe.holders),
    }
    if include_layout:
        result["partitions"] = [part.to_dict() for part in probe.partitions]
        result["mbr_table_bytes"] = probe.mbr_table_bytes
        result["gpt_signature"] = probe.gpt_signature
    # Use the same array/object types that survive a JSON plan or backup round trip.
    return json.loads(json.dumps(result, sort_keys=True))


def _require_same_probe(expected: DeviceProbe, actual: DeviceProbe, *, include_layout: bool = True) -> None:
    if _probe_identity(expected, include_layout=include_layout) != _probe_identity(
        actual, include_layout=include_layout
    ):
        raise InstallerError("target identity changed after the plan was created")


def _mbr_partition_bytes(total_sectors: int) -> bytes:
    if total_sectors != EMMC_BYTES // SECTOR_SIZE:
        raise InstallerError("cannot initialize an unvalidated target capacity")
    root_sectors = total_sectors - ROOT_START_SECTOR
    if root_sectors <= ROOT_IMAGE_BYTES // SECTOR_SIZE:
        raise InstallerError("root partition is too small for the payload")

    def entry(part_type: int, start: int, sectors: int) -> bytes:
        # LBA fields are authoritative. Saturated CHS avoids geometry-dependent values.
        return struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff", part_type,
                           b"\xfe\xff\xff", start, sectors)

    table = bytearray(66)
    table[0:16] = entry(0x0C, BOOT_START_SECTOR, BOOT_SECTORS)
    table[16:32] = entry(0x83, ROOT_START_SECTOR, root_sectors)
    table[64:66] = b"\x55\xaa"
    return bytes(table)


def _read_partition_table_state(
    path: str, size_bytes: int, sector_size: int
) -> tuple[str, bool]:
    if (
        sector_size < 512
        or size_bytes < sector_size * 2
        or size_bytes % sector_size != 0
    ):
        raise InstallerError("target geometry is invalid for partition-table probing")
    descriptor = os.open(path, os.O_RDONLY)
    try:
        first_two = os.pread(descriptor, sector_size * 2, 0)
        last_sector = os.pread(descriptor, sector_size, size_bytes - sector_size)
    finally:
        os.close(descriptor)
    if len(first_two) != sector_size * 2 or len(last_sector) != sector_size:
        raise InstallerError("cannot read target MBR/GPT probe bytes")
    primary_gpt = first_two[sector_size : sector_size + 8] == b"EFI PART"
    backup_gpt = last_sector[:8] == b"EFI PART"
    return first_two[446:512].hex(), primary_gpt or backup_gpt


def _materialize_artifact(release: Release, artifact: Artifact, destination: Path) -> None:
    source = Path(release.directory) / artifact.file
    compressed_digest = hashlib.sha256()
    raw_digest = hashlib.sha256()
    compressed_size = 0
    raw_size = 0

    class HashingReader:
        def __init__(self, handle: object) -> None:
            self.handle = handle

        def read(self, size: int = -1) -> bytes:
            nonlocal compressed_size
            data = self.handle.read(size)
            compressed_digest.update(data)
            compressed_size += len(data)
            return data

        def readable(self) -> bool:
            return True

    try:
        with source.open("rb") as raw_handle, destination.open("xb") as output:
            with gzip.GzipFile(fileobj=HashingReader(raw_handle), mode="rb") as archive:
                while chunk := archive.read(4 * 1024 * 1024):
                    raw_size += len(chunk)
                    if raw_size > artifact.raw_size:
                        raise InstallerError(f"{artifact.file} expands beyond declared raw_size")
                    raw_digest.update(chunk)
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise InstallerError(f"failed to materialize {artifact.file}: {exc}") from exc
    if compressed_size != artifact.size or compressed_digest.hexdigest() != artifact.sha256:
        raise InstallerError(f"compressed artifact changed during extraction: {artifact.file}")
    if raw_size != artifact.raw_size or raw_digest.hexdigest() != artifact.raw_sha256:
        raise InstallerError(f"raw artifact verification failed: {artifact.file}")


def _rewrite_fstab(root: Path, root_uuid: str) -> None:
    if not UUID_RE.fullmatch(root_uuid):
        raise InstallerError("generated root UUID is not canonical")
    path = root / "etc/fstab"
    try:
        old_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise InstallerError(f"cannot read target fstab: {exc}") from exc
    kept: list[str] = []
    for line in old_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        fields = stripped.split()
        if len(fields) >= 2 and fields[1] in {"/", "/boot"}:
            continue
        kept.append(line)
    kept.extend(
        [
            f"UUID={root_uuid} / ext4 defaults,noatime,nodiratime,commit=600,errors=remount-ro 0 1",
            "LABEL=BOOT_EMMC /boot vfat defaults 0 2",
        ]
    )
    _atomic_write(path, ("\n".join(kept).rstrip() + "\n").encode("utf-8"), 0o644)


def _rewrite_uenv(boot_root: Path, root_uuid: str) -> None:
    path = boot_root / "uEnv.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise InstallerError(f"cannot read boot uEnv.txt: {exc}") from exc
    found = 0
    result: list[str] = []
    for line in lines:
        if line.startswith("APPEND="):
            found += 1
            words = line[len("APPEND=") :].split()
            indexes = [index for index, word in enumerate(words) if word.startswith("root=")]
            if len(indexes) != 1:
                raise InstallerError("uEnv.txt APPEND must contain exactly one root= argument")
            words[indexes[0]] = f"root=UUID={root_uuid}"
            result.append("APPEND=" + " ".join(words))
        else:
            result.append(line)
    if found != 1:
        raise InstallerError("uEnv.txt must contain exactly one APPEND line")
    _atomic_write(path, ("\n".join(result) + "\n").encode("utf-8"), 0o644)


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


class SystemExecutor:
    """All privileged and device-specific operations used by the CLI."""

    def run(self, argv: Sequence[str], *, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(list(argv), text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
        if result.returncode not in ok:
            detail = result.stderr.strip() or result.stdout.strip()
            raise InstallerError(f"command failed ({result.returncode}): {argv!r}: {detail}")
        return result

    @staticmethod
    def _read_sysfs(path: Path) -> str:
        try:
            return path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise InstallerError(f"cannot read {path}: {exc}") from exc

    def _block_size(self, path: str) -> int:
        return int(self.run(["blockdev", "--getsize64", path]).stdout.strip())

    def _root_disk(self) -> str:
        source = self.run(["findmnt", "-n", "-o", "SOURCE", "/"]).stdout.strip().split("[", 1)[0]
        source = os.path.realpath(source)
        parent = self.run(["lsblk", "-ndo", "PKNAME", source]).stdout.strip()
        return f"/dev/{parent}" if parent else source

    @staticmethod
    def _dt_compatible() -> tuple[str, ...]:
        try:
            return tuple(
                item.decode("ascii")
                for item in Path("/proc/device-tree/compatible").read_bytes().split(b"\0")
                if item
            )
        except (OSError, UnicodeError) as exc:
            raise InstallerError(f"cannot read device-tree compatible: {exc}") from exc

    @staticmethod
    def _dt_ram_bytes() -> int:
        root = Path("/proc/device-tree")
        try:
            address_cells = int.from_bytes((root / "#address-cells").read_bytes(), "big")
            size_cells = int.from_bytes((root / "#size-cells").read_bytes(), "big")
            memory_nodes = sorted(root.glob("memory*"))
            if not memory_nodes:
                raise InstallerError("device tree has no memory node")
            reg = (memory_nodes[0] / "reg").read_bytes()
        except OSError as exc:
            raise InstallerError(f"cannot read device-tree memory: {exc}") from exc
        stride = 4 * (address_cells + size_cells)
        if stride <= 0 or len(reg) % stride:
            raise InstallerError("device-tree memory reg has an invalid shape")
        total = 0
        for offset in range(0, len(reg), stride):
            size_offset = offset + 4 * address_cells
            total += int.from_bytes(reg[size_offset : size_offset + 4 * size_cells], "big")
        return total

    def probe(self, target: str) -> DeviceProbe:
        resolved = os.path.realpath(target)
        try:
            mode = os.stat(resolved).st_mode
        except OSError as exc:
            raise InstallerError(f"cannot stat target {target}: {exc}") from exc
        if not stat.S_ISBLK(mode):
            raise InstallerError("production target must be a block device")
        name = Path(resolved).name
        if name.startswith("loop") or name.startswith("dm-") or name.startswith("md"):
            raise InstallerError("loop, device-mapper, and RAID targets are forbidden")
        sysfs = Path("/sys/class/block") / name
        if not sysfs.exists():
            raise InstallerError("target has no sysfs block-device entry")
        if (sysfs / "partition").exists():
            raise InstallerError("target must be a whole MMC user device")
        device_type = self._read_sysfs(sysfs / "device/type")
        product = self._read_sysfs(sysfs / "device/name")
        cid = self._read_sysfs(sysfs / "device/cid").lower()
        if re.fullmatch(r"[0-9a-f]{32}", cid) is None:
            raise InstallerError("MMC CID is not a 32-digit hexadecimal value")
        removable = self._read_sysfs(sysfs / "removable") != "0"
        sector_size = int(self._read_sysfs(sysfs / "queue/logical_block_size"))
        size_bytes = int(self._read_sysfs(sysfs / "size")) * 512
        boot0, boot1 = f"/dev/{name}boot0", f"/dev/{name}boot1"
        if not Path(boot0).exists() or not Path(boot1).exists():
            boot0 = boot1 = ""

        lsblk = json.loads(
            self.run(["lsblk", "--json", "-b", "-o",
                      "NAME,PATH,TYPE,SIZE,START,PKNAME,MOUNTPOINTS", resolved]).stdout
        )
        blockdevices = lsblk.get("blockdevices", [])
        if len(blockdevices) != 1:
            raise InstallerError("lsblk did not return exactly one target")
        partitions: list[Partition] = []
        all_names = {resolved}
        whole_mounts = tuple(str(x) for x in (blockdevices[0].get("mountpoints") or []) if x)
        for child in blockdevices[0].get("children", []) or []:
            if child.get("type") != "part":
                raise InstallerError("target has a non-partition child device")
            child_path = os.path.realpath(str(child["path"]))
            match = re.fullmatch(re.escape(resolved) + r"p([0-9]+)", child_path)
            if match is None:
                raise InstallerError("unexpected MMC partition name")
            mounts = tuple(str(x) for x in (child.get("mountpoints") or []) if x)
            child_sysfs = Path("/sys/class/block") / Path(child_path).name
            holders = tuple(sorted(item.name for item in (child_sysfs / "holders").glob("*")))
            partitions.append(
                Partition(int(match.group(1)), child_path, int(child["start"]),
                          int(child["size"]) // sector_size, mounts, holders)
            )
            all_names.add(child_path)
        swaps = tuple(
            line.strip() for line in self.run(
                ["swapon", "--show", "--noheadings", "--raw", "--output", "NAME"]
            ).stdout.splitlines() if os.path.realpath(line.strip()) in all_names
        )
        holders = tuple(sorted(item.name for item in (sysfs / "holders").glob("*")))
        mbr_table_bytes, gpt_signature = _read_partition_table_state(
            resolved, size_bytes, sector_size
        )
        return DeviceProbe(
            path=resolved,
            major_minor=self._read_sysfs(sysfs / "dev"),
            sysfs_path=str(sysfs.resolve()),
            cid=cid,
            product=product,
            size_bytes=size_bytes,
            sector_size=sector_size,
            removable=removable,
            device_type=device_type,
            compatible=self._dt_compatible(),
            ram_bytes=self._dt_ram_bytes(),
            boot0=boot0,
            boot1=boot1,
            boot0_bytes=self._block_size(boot0) if boot0 else 0,
            boot1_bytes=self._block_size(boot1) if boot1 else 0,
            partitions=tuple(sorted(partitions, key=lambda item: item.number)),
            root_disk=self._root_disk(),
            mounts=whole_mounts,
            swap_devices=swaps,
            holders=holders,
            mbr_table_bytes=mbr_table_bytes,
            gpt_signature=gpt_signature,
        )

    @staticmethod
    def require_usb_media() -> None:
        marker = Path("/etc/m17s-armbian/media")
        try:
            value = marker.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise InstallerError(f"cannot read installation-media marker: {exc}") from exc
        if value != "usb":
            raise InstallerError("installer may run only from an M17S USB image (media=usb)")

    def hash_range(self, path: str, offset: int, length: int) -> str:
        digest = hashlib.sha256()
        remaining = length
        descriptor = os.open(path, os.O_RDONLY)
        try:
            position = offset
            while remaining:
                chunk = os.pread(descriptor, min(4 * 1024 * 1024, remaining), position)
                if not chunk:
                    raise InstallerError(f"short read while hashing {path}")
                digest.update(chunk)
                position += len(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
        return digest.hexdigest()

    def write_at(self, path: str, offset: int, data: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_SYNC)
        try:
            written = os.pwrite(descriptor, data, offset)
            if written != len(data):
                raise InstallerError(f"short write to {path}")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def reread_partitions(self, path: str) -> None:
        self.run(["blockdev", "--rereadpt", path])
        self.run(["udevadm", "settle"])

    def write_image(self, source: Path, target: str, raw_size: int, raw_sha256: str) -> None:
        if source.stat().st_size != raw_size:
            raise InstallerError("materialized image size changed before writing")
        if self._block_size(target) < raw_size:
            raise InstallerError(f"target partition is too small: {target}")
        source_digest, _ = _sha256_file(source)
        if source_digest != raw_sha256:
            raise InstallerError("materialized image hash changed before writing")
        with source.open("rb") as input_handle:
            descriptor = os.open(target, os.O_WRONLY | os.O_SYNC)
            try:
                total = 0
                while chunk := input_handle.read(4 * 1024 * 1024):
                    view = memoryview(chunk)
                    while view:
                        count = os.write(descriptor, view)
                        if count <= 0:
                            raise InstallerError(f"short write to {target}")
                        view = view[count:]
                    total += len(chunk)
                if total != raw_size:
                    raise InstallerError(f"short source image while writing {target}")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if self.hash_range(target, 0, raw_size) != raw_sha256:
            raise InstallerError(f"raw readback verification failed: {target}")

    def prepare_ext4(self, path: str) -> str:
        self.run(["e2fsck", "-f", "-p", path], ok=(0, 1))
        self.run(["resize2fs", path])
        self.run(["tune2fs", "-U", "random", path])
        self.run(["e2fsck", "-f", "-p", path], ok=(0, 1))
        self.run(["udevadm", "settle"])
        value = self.run(["blkid", "-p", "-s", "UUID", "-o", "value", path]).stdout.strip().lower()
        if not UUID_RE.fullmatch(value):
            raise InstallerError("blkid did not return a canonical ext4 UUID")
        return value

    def require_fat_label(self, path: str, expected: str) -> None:
        actual = self.run(["blkid", "-p", "-s", "LABEL", "-o", "value", path]).stdout.strip()
        if actual != expected:
            raise InstallerError(f"boot filesystem label must be {expected}, got {actual!r}")

    @contextlib.contextmanager
    def mounted(self, device: str, *, readonly: bool = False) -> Iterator[Path]:
        root = Path(tempfile.mkdtemp(prefix="m17s-mount-", dir="/run"))
        options = "ro,nodev,nosuid,noexec" if readonly else "rw,nodev,nosuid,noexec"
        try:
            self.run(["mount", "-o", options, device, str(root)])
            yield root
            self.run(["sync", "-f", str(root)])
        finally:
            if self.run(["findmnt", "-n", str(root)], ok=(0, 1)).returncode == 0:
                self.run(["umount", str(root)])
            root.rmdir()

    def backing_disk(self, path: Path) -> str | None:
        source = self.run(["findmnt", "-n", "-o", "SOURCE", "-T", str(path)]).stdout.strip().split("[", 1)[0]
        if not source.startswith("/dev/"):
            return None
        source = os.path.realpath(source)
        parent = self.run(["lsblk", "-ndo", "PKNAME", source]).stdout.strip()
        return f"/dev/{parent}" if parent else source

    @staticmethod
    def free_bytes(path: Path) -> int:
        return shutil.disk_usage(path).free

    def backup_gzip(self, source: str, destination: Path, raw_size: int) -> dict[str, object]:
        raw_digest = hashlib.sha256()
        read_size = 0
        with open(source, "rb", buffering=0) as input_handle, destination.open("xb") as output_handle:
            with gzip.GzipFile(fileobj=output_handle, mode="wb", filename="", mtime=0) as archive:
                while read_size < raw_size:
                    chunk = input_handle.read(min(4 * 1024 * 1024, raw_size - read_size))
                    if not chunk:
                        raise InstallerError(f"short read while backing up {source}")
                    archive.write(chunk)
                    raw_digest.update(chunk)
                    read_size += len(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.chmod(destination, 0o600)
        compressed_sha256, compressed_size = _sha256_file(destination)
        entry = {
            "file": destination.name,
            "source": source,
            "raw_size": raw_size,
            "raw_sha256": raw_digest.hexdigest(),
            "size": compressed_size,
            "sha256": compressed_sha256,
        }
        self.verify_backup_entry(destination.parent, entry)
        return entry

    def verify_backup_entry(self, directory: Path, entry: Mapping[str, object]) -> None:
        path = directory / str(entry["file"])
        digest, size = _sha256_file(path)
        if digest != entry["sha256"] or size != entry["size"]:
            raise InstallerError(f"compressed backup verification failed: {path.name}")
        raw_digest = hashlib.sha256()
        raw_size = 0
        try:
            with gzip.open(path, "rb") as handle:
                while chunk := handle.read(4 * 1024 * 1024):
                    raw_digest.update(chunk)
                    raw_size += len(chunk)
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise InstallerError(f"cannot verify backup {path.name}: {exc}") from exc
        if raw_size != entry["raw_size"] or raw_digest.hexdigest() != entry["raw_sha256"]:
            raise InstallerError(f"raw backup verification failed: {path.name}")


def save_plan(path: os.PathLike[str] | str, plan: InstallPlan) -> None:
    _write_json(Path(path), plan.to_dict())


def load_plan(path: os.PathLike[str] | str) -> InstallPlan:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError(f"cannot read plan: {exc}") from exc
    plan = InstallPlan.from_dict(value)
    if plan.schema_version != SCHEMA_VERSION or plan.board != BOARD:
        raise InstallerError("plan schema or board does not match this installer")
    return plan


def create_backup(plan: InstallPlan, output: os.PathLike[str] | str,
                  executor: SystemExecutor) -> dict[str, object]:
    actual = executor.probe(plan.target.path)
    _require_same_probe(plan.target, actual)
    _validate_layout(actual, plan.initialize_layout)
    directory = Path(output).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    if executor.backing_disk(directory) == actual.path:
        raise InstallerError("backup destination is on the target eMMC")
    required_space = actual.size_bytes + actual.boot0_bytes + actual.boot1_bytes + 64 * 1024 * 1024
    if executor.free_bytes(directory) < required_space:
        raise InstallerError("backup destination lacks worst-case space for a complete verified backup")
    entries = []
    for label, source, size in (
        ("user", actual.path, actual.size_bytes),
        ("boot0", actual.boot0, actual.boot0_bytes),
        ("boot1", actual.boot1, actual.boot1_bytes),
    ):
        entries.append(executor.backup_gzip(source, directory / f"{label}.img.gz", size))
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "board": BOARD,
        "cid": actual.cid,
        "target_size": actual.size_bytes,
        "target_identity": _probe_identity(actual),
        "entries": entries,
    }
    _write_json(directory / "backup.json", manifest)
    return manifest


def _load_and_verify_backup(path: os.PathLike[str] | str, plan: InstallPlan,
                            executor: SystemExecutor) -> dict[str, object]:
    manifest_path = Path(path).resolve()
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError(f"cannot read backup manifest: {exc}") from exc
    if value.get("schema_version") != SCHEMA_VERSION or value.get("board") != BOARD:
        raise InstallerError("backup schema or board does not match")
    if value.get("cid") != plan.target.cid or value.get("target_size") != plan.target.size_bytes:
        raise InstallerError("backup is not bound to this target CID and capacity")
    if value.get("target_identity") != _probe_identity(plan.target):
        raise InstallerError("backup target snapshot does not match the saved install plan")
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) != 3:
        raise InstallerError("backup manifest must contain user, boot0, and boot1 images")
    expected = {
        plan.target.path: plan.target.size_bytes,
        plan.target.boot0: plan.target.boot0_bytes,
        plan.target.boot1: plan.target.boot1_bytes,
    }
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("source") not in expected:
            raise InstallerError("backup contains an unexpected source")
        filename = str(entry.get("file", ""))
        if not filename or Path(filename).name != filename:
            raise InstallerError("backup file must be a basename inside the backup directory")
        source = str(entry["source"])
        if source in seen or int(entry.get("raw_size", -1)) != expected[source]:
            raise InstallerError("backup source is duplicated or has the wrong size")
        executor.verify_backup_entry(manifest_path.parent, entry)
        seen.add(source)
    if seen != set(expected):
        raise InstallerError("backup set is incomplete")
    return value


def _protected_hashes(executor: SystemExecutor, probe: DeviceProbe,
                      initialize_layout: bool) -> dict[str, str]:
    values = {
        "boot0": executor.hash_range(probe.boot0, 0, probe.boot0_bytes),
        "boot1": executor.hash_range(probe.boot1, 0, probe.boot1_bytes),
    }
    if initialize_layout:
        values["mbr_bootcode"] = executor.hash_range(probe.path, 0, 446)
        values["signed_prefix"] = executor.hash_range(probe.path, 512, PREFIX_BYTES - 512)
    else:
        values["whole_prefix"] = executor.hash_range(probe.path, 0, PREFIX_BYTES)
    gap_offset = (BOOT_START_SECTOR + BOOT_SECTORS) * SECTOR_SIZE
    gap_length = (ROOT_START_SECTOR - BOOT_START_SECTOR - BOOT_SECTORS) * SECTOR_SIZE
    values["partition_gap"] = executor.hash_range(probe.path, gap_offset, gap_length)
    return values


def _prepare_generated_boot_files(
    boot_root: Path,
    root_uuid: str,
    generator: Callable[..., dict[str, bytes]],
    provision: firstboot.ProvisionConfig | None,
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    active = boot_root / "emmc_autoscript"
    if active.exists():
        raise InstallerError("boot payload must not contain an active emmc_autoscript")
    if (boot_root / "firstboot.json").exists():
        raise InstallerError("boot payload must not contain a user firstboot.json")
    required = {
        "original U-Boot": boot_root / "u-boot-p212.bin",
        "zImage": boot_root / "zImage",
        "uInitrd": boot_root / "uInitrd",
        "DTB": boot_root / DTB_RELATIVE,
    }
    for label, path in required.items():
        if not path.is_file():
            raise InstallerError(f"boot payload is missing {label}: {path.relative_to(boot_root)}")
    generated = generator(
        original_uboot=required["original U-Boot"].read_bytes(),
        root_uuid=root_uuid,
        kernel=required["zImage"].read_bytes(),
        initrd=required["uInitrd"].read_bytes(),
        dtb=required["DTB"].read_bytes(),
    )
    expected = {
        "u-boot-m17s-ram.bin", "emmc_autoscript.cmd", "emmc_autoscript",
        "m17s-ramboot.cmd", "m17s-ramboot.scr", "manifest.json",
    }
    if set(generated) != expected:
        raise InstallerError("boot generator returned an unexpected file set")
    # Install only passive files. Activation is a distinct post-hash transaction.
    expected_files: dict[str, bytes] = {}
    for name in sorted(expected - {"emmc_autoscript"}):
        _atomic_write(boot_root / name, generated[name])
        expected_files[name] = generated[name]
    _rewrite_uenv(boot_root, root_uuid)
    expected_files["uEnv.txt"] = (boot_root / "uEnv.txt").read_bytes()
    if provision is not None:
        config = {
            "hostname": provision.hostname,
            "username": provision.username,
            "ssh_authorized_keys": list(provision.ssh_authorized_keys),
        }
        firstboot_data = (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(boot_root / "firstboot.json", firstboot_data, 0o600)
        expected_files["firstboot.json"] = firstboot_data
    return generated, expected_files


def _activate_boot(boot_root: Path, image: bytes) -> None:
    active = boot_root / "emmc_autoscript"
    if active.exists():
        raise InstallerError("refusing to replace an unexpectedly active emmc_autoscript")
    _atomic_write(active, image)


def apply_plan(plan: InstallPlan, release: Release, backup_manifest: os.PathLike[str] | str,
               executor: SystemExecutor, *, confirmation: str,
               work_dir: os.PathLike[str] | str,
               firstboot_config: os.PathLike[str] | str | None = None,
               materializer: Callable[[Release, Artifact, Path], None] = _materialize_artifact,
               generator: Callable[..., dict[str, bytes]] = boot.generate_boot_files) -> dict[str, object]:
    if release.manifest_sha256 != plan.release_manifest_sha256 or release.version != plan.release_version:
        raise InstallerError("release does not match the saved install plan")
    if release.boot_artifact != plan.boot_artifact or release.root_artifact != plan.root_artifact:
        raise InstallerError("release artifacts do not match the saved install plan")
    actual = executor.probe(plan.target.path)
    _require_same_probe(plan.target, actual)
    _validate_layout(actual, plan.initialize_layout)
    _load_and_verify_backup(backup_manifest, plan, executor)
    token = f"ERASE {actual.path} {actual.cid}"
    if confirmation != token:
        raise InstallerError(f"confirmation must exactly match: {token}")
    provision = None
    if firstboot_config is not None:
        try:
            provision = firstboot.parse_config(firstboot_config)
        except ValueError as exc:
            raise InstallerError(f"invalid firstboot config: {exc}") from exc
        if provision is None or not provision.ssh_authorized_keys:
            raise InstallerError("--firstboot-config must contain at least one validated SSH public key")
    work_parent = Path(work_dir).resolve()
    if not work_parent.is_dir():
        raise InstallerError("--work-dir must name an existing external directory")
    if executor.backing_disk(work_parent) == actual.path:
        raise InstallerError("--work-dir must not reside on the target eMMC")
    required_work = BOOT_IMAGE_BYTES + ROOT_IMAGE_BYTES + 64 * 1024 * 1024
    if executor.free_bytes(work_parent) < required_work:
        raise InstallerError("--work-dir lacks space for both fully expanded payloads")

    with tempfile.TemporaryDirectory(prefix="m17s-payload-", dir=work_parent) as temporary:
        temp = Path(temporary)
        boot_image = temp / "boot.img"
        root_image = temp / "root.img"
        # No target write occurs until both gzip streams have been fully verified.
        materializer(release, release.boot_artifact, boot_image)
        materializer(release, release.root_artifact, root_image)
        final_probe = executor.probe(actual.path)
        _require_same_probe(plan.target, final_probe)
        _validate_layout(final_probe, plan.initialize_layout)
        actual = final_probe
        before = _protected_hashes(executor, actual, plan.initialize_layout)

        if plan.initialize_layout:
            executor.write_at(actual.path, 446, _mbr_partition_bytes(actual.size_bytes // SECTOR_SIZE))
            executor.reread_partitions(actual.path)
            initialized = executor.probe(actual.path)
            _validate_layout(initialized, False)
            _require_same_probe(actual, initialized, include_layout=False)
            actual = initialized

        parts = {part.number: part for part in actual.partitions}
        root_part, boot_part = parts[2].path, parts[1].path
        executor.write_image(root_image, root_part, release.root_artifact.raw_size,
                             release.root_artifact.raw_sha256)
        root_uuid = executor.prepare_ext4(root_part)
        with executor.mounted(root_part, readonly=False) as root_mount:
            _rewrite_fstab(root_mount, root_uuid)
            media = root_mount / "etc/m17s-armbian/media"
            media.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(media, b"emmc\n", 0o644)
        with executor.mounted(root_part, readonly=True) as root_mount:
            fstab = (root_mount / "etc/fstab").read_text(encoding="utf-8")
            if f"UUID={root_uuid} / ext4 " not in fstab or "LABEL=BOOT_EMMC /boot vfat " not in fstab:
                raise InstallerError("target fstab readback verification failed")
            if (root_mount / "etc/m17s-armbian/media").read_text(encoding="ascii") != "emmc\n":
                raise InstallerError("target media marker readback verification failed")

        executor.write_image(boot_image, boot_part, release.boot_artifact.raw_size,
                             release.boot_artifact.raw_sha256)
        executor.require_fat_label(boot_part, "BOOT_EMMC")
        with executor.mounted(boot_part, readonly=False) as boot_mount:
            generated, passive_files = _prepare_generated_boot_files(
                boot_mount, root_uuid, generator, provision
            )

        with executor.mounted(boot_part, readonly=True) as boot_mount:
            if (boot_mount / "emmc_autoscript").exists():
                raise InstallerError("activation appeared before protected-state verification")
            for name, expected_data in sorted(passive_files.items()):
                if (boot_mount / name).read_bytes() != expected_data:
                    raise InstallerError(f"passive boot-file readback mismatch: {name}")

        # No vendor-visible activation file exists yet. Prove raw protected state
        # before making the new eMMC installation bootable.
        pre_activation = _protected_hashes(executor, actual, plan.initialize_layout)
        if before != pre_activation:
            raise InstallerError("PROTECTED_REGION_CHANGED_BEFORE_ACTIVATION")
        with executor.mounted(boot_part, readonly=False) as boot_mount:
            _activate_boot(boot_mount, generated["emmc_autoscript"])

        # Re-open read-only and prove that activation and every generated byte survived unmount.
        final_files = dict(passive_files)
        final_files["emmc_autoscript"] = generated["emmc_autoscript"]
        with executor.mounted(boot_part, readonly=True) as boot_mount:
            for name, expected_data in sorted(final_files.items()):
                if (boot_mount / name).read_bytes() != expected_data:
                    raise InstallerError(f"final boot-file readback mismatch: {name}")
        after = _protected_hashes(executor, actual, plan.initialize_layout)
        if before != after:
            raise InstallerError("PROTECTED_REGION_CHANGED")
    return {
        "status": "installed",
        "target": actual.path,
        "cid": actual.cid,
        "root_uuid": root_uuid,
        "initialized_layout": plan.initialize_layout,
        "protected_hashes": after,
    }


def _json_print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M17S vendor-boot-preserving eMMC installer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe_parser = subparsers.add_parser("probe")
    probe_parser.add_argument("--target", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--target", required=True)
    plan_parser.add_argument("--release-dir", required=True)
    plan_parser.add_argument("--output", required=True)
    plan_parser.add_argument("--initialize-layout", action="store_true")
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--plan", required=True)
    backup_parser.add_argument("--output", required=True)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--plan", required=True)
    apply_parser.add_argument("--release-dir", required=True)
    apply_parser.add_argument("--backup-manifest", required=True)
    apply_parser.add_argument("--work-dir", required=True)
    apply_parser.add_argument("--firstboot-config")
    apply_parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    executor = SystemExecutor()
    try:
        executor.require_usb_media()
        if args.command == "probe":
            probe = executor.probe(args.target)
            _json_print(probe.to_dict())
            return 0
        if args.command == "plan":
            release = load_release(args.release_dir)
            plan = create_plan(release, executor.probe(args.target),
                               initialize_layout=args.initialize_layout)
            save_plan(args.output, plan)
            _json_print(plan.to_dict())
            return 0
        if args.command == "backup":
            if os.geteuid() != 0:
                raise InstallerError("backup must run as root")
            _json_print(create_backup(load_plan(args.plan), args.output, executor))
            return 0
        if args.command == "apply":
            if not args.apply:
                raise InstallerError("apply requires the explicit --apply flag")
            if os.geteuid() != 0:
                raise InstallerError("apply must run as root")
            plan = load_plan(args.plan)
            token = f"ERASE {plan.target.path} {plan.target.cid}"
            print(f"Destructive confirmation required. Type exactly:\n{token}", file=sys.stderr)
            confirmation = input("> ")
            result = apply_plan(plan, load_release(args.release_dir), args.backup_manifest,
                                executor, confirmation=confirmation, work_dir=args.work_dir,
                                firstboot_config=args.firstboot_config)
            _json_print(result)
            return 0
        raise InstallerError("unknown command")
    except InstallerError as exc:
        print(f"m17s-emmc: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
