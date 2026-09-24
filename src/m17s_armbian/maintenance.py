"""Verify and safely stage M17S eMMC boot bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import struct
import sys
import zlib

from . import boot


_OUTPUT_NAMES = {
    "u-boot-m17s-ram.bin",
    "emmc_autoscript.cmd",
    "emmc_autoscript",
    "m17s-ramboot.cmd",
    "m17s-ramboot.scr",
}
_KERNEL_VERSION = re.compile(rb"Linux version ([0-9A-Za-z][0-9A-Za-z._+-]{0,127}) ")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def kernel_release(kernel: bytes) -> str:
    """Extract the release from the ARM64 Image's build banner."""
    boot.validate_arm64_image(kernel)
    matches = {match.group(1).decode("ascii") for match in _KERNEL_VERSION.finditer(kernel)}
    if len(matches) != 1:
        raise ValueError("kernel must contain exactly one unambiguous Linux version release")
    return matches.pop()


def _script_command(image: bytes) -> bytes:
    if len(image) < 72:
        raise ValueError("legacy script image is too short")
    fields = struct.unpack(">7I4B32s", image[:64])
    magic, header_crc, timestamp, data_size, load, entry, data_crc = fields[:7]
    if magic != 0x27051956 or fields[7:11] != (5, 2, 6, 0):
        raise ValueError("legacy script image has invalid type metadata")
    header = bytearray(image[:64])
    header[4:8] = b"\0" * 4
    if zlib.crc32(header) & 0xFFFFFFFF != header_crc:
        raise ValueError("legacy script header CRC mismatch")
    data = image[64:]
    if data_size != len(data) or zlib.crc32(data) & 0xFFFFFFFF != data_crc:
        raise ValueError("legacy script data size or CRC mismatch")
    command_size, zero = struct.unpack(">II", data[:8])
    if timestamp != 0 or load != 0 or entry != 0 or zero != 0:
        raise ValueError("legacy script does not use the deterministic M17S header")
    if command_size != len(data) - 8:
        raise ValueError("legacy script command length mismatch")
    return data[8:]


def _active_prefix(top_level_script: bytes) -> str:
    command = _script_command(top_level_script)
    match = re.search(rb"fatload mmc 1:1 0x08080000 /([^\s;]*/)?(?:Image|zImage);", command)
    if match is None:
        raise ValueError("active emmc_autoscript has no recognized kernel load path")
    raw = (match.group(1) or b"").rstrip(b"/")
    try:
        prefix = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("active payload prefix is not ASCII") from exc
    return boot.validate_payload_prefix(prefix)


def _regular_bytes(path: Path, label: str) -> bytes:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"missing {label}: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular file: {path}")
    return path.read_bytes()


def _recover_original(patched: bytes) -> bytes:
    if len(patched) != boot.ORIGINAL_UBOOT_SIZE:
        raise ValueError("patched U-Boot has the wrong size")
    if patched.count(boot.NEW_BOOTCMD) != 1 or patched.count(boot.OLD_BOOTCMD) != 0:
        raise ValueError("patched U-Boot does not contain the unique M17S bootcmd")
    original = patched.replace(boot.NEW_BOOTCMD, boot.OLD_BOOTCMD, 1)
    boot.validate_original_uboot(original)
    return original


def _load_verified_bundle(boot_dir: Path) -> dict[str, object]:
    boot_dir = boot_dir.resolve()
    top_script = _regular_bytes(boot_dir / "emmc_autoscript", "active emmc_autoscript")
    prefix = _active_prefix(top_script)
    bundle_dir = boot_dir.joinpath(*prefix.split("/")) if prefix else boot_dir
    manifest_bytes = _regular_bytes(bundle_dir / "manifest.json", "bundle manifest")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("bundle manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise ValueError("unsupported bundle manifest format")
    if manifest.get("payload_prefix") != prefix:
        raise ValueError("manifest payload_prefix does not match the active script")
    root_uuid = boot.validate_root_uuid(manifest.get("root_uuid"))
    extra_args = manifest.get("extra_args", "")
    boot.validate_extra_args(extra_args)

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != _OUTPUT_NAMES:
        raise ValueError("manifest output file set is incomplete or unexpected")
    output_data: dict[str, bytes] = {}
    for name in sorted(_OUTPUT_NAMES):
        metadata = outputs[name]
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid manifest metadata for {name}")
        data = _regular_bytes(bundle_dir / name, name)
        if metadata.get("bytes") != len(data) or metadata.get("sha256") != _hash(data):
            raise ValueError(f"manifest hash or size mismatch for {name}")
        output_data[name] = data
    if top_script != output_data["emmc_autoscript"]:
        raise ValueError("active emmc_autoscript does not match its slot copy")

    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("manifest inputs are missing")
    input_paths = {
        "kernel": bundle_dir / ("Image" if prefix else "zImage"),
        "uInitrd": bundle_dir / "uInitrd",
        boot.DTB_PATH: bundle_dir / boot.DTB_PATH.lstrip("/"),
    }
    if set(inputs) != set(input_paths):
        raise ValueError("manifest input file set is incomplete or unexpected")
    input_data: dict[str, bytes] = {}
    for name, path in input_paths.items():
        metadata = inputs[name]
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid manifest metadata for {name}")
        data = _regular_bytes(path, name)
        if metadata.get("bytes") != len(data) or metadata.get("sha256") != _hash(data):
            raise ValueError(f"manifest hash or size mismatch for {name}")
        input_data[name] = data

    original = _recover_original(output_data["u-boot-m17s-ram.bin"])
    regenerated = boot.generate_boot_files(
        original,
        root_uuid,
        input_data["kernel"],
        input_data["uInitrd"],
        input_data[boot.DTB_PATH],
        extra_args,
        payload_prefix=prefix,
    )
    for name in _OUTPUT_NAMES | {"manifest.json"}:
        actual = manifest_bytes if name == "manifest.json" else output_data[name]
        if actual != regenerated[name]:
            raise ValueError(f"bundle file is not reproducible from its manifest: {name}")
    return {
        "boot_dir": boot_dir,
        "bundle_dir": bundle_dir,
        "prefix": prefix,
        "manifest": manifest,
        "original_uboot": original,
        "kernel": input_data["kernel"],
    }


def verify_bundle(boot_dir: str | os.PathLike[str] = "/boot") -> dict[str, object]:
    """Verify the active top-level script and every file in its selected bundle."""
    state = _load_verified_bundle(Path(boot_dir))
    manifest = state["manifest"]
    return {
        "status": "ok",
        "boot_dir": str(state["boot_dir"]),
        "bundle_dir": str(state["bundle_dir"]),
        "payload_prefix": state["prefix"],
        "root_uuid": manifest["root_uuid"],
        "kernel_release": kernel_release(state["kernel"]),
        "files_verified": 10,
    }


def _unescape_mount_path(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value
    )


def _mount_fstype(path: Path, mountinfo_path: Path) -> str | None:
    target = path.resolve()
    best: tuple[int, str] | None = None
    try:
        lines = mountinfo_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read mount table: {mountinfo_path}") from exc
    for line in lines:
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        trailing = right.split()
        if len(fields) < 5 or not trailing:
            continue
        mountpoint = Path(_unescape_mount_path(fields[4])).resolve()
        try:
            target.relative_to(mountpoint)
        except ValueError:
            continue
        candidate = (len(mountpoint.parts), trailing[0])
        if best is None or candidate[0] > best[0]:
            best = candidate
    return None if best is None else best[1]


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_replace_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.m17s-{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f"refusing to replace existing temporary path: {temporary}")
    try:
        _write_file(temporary, data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _slot_digest(
    kernel: bytes, initrd: bytes, dtb: bytes, root_uuid: str, extra_args: str
) -> str:
    """Hash the complete boot identity with unambiguous field boundaries."""
    digest = hashlib.sha256()
    fields = (
        ("kernel", kernel),
        ("initrd", initrd),
        ("dtb", dtb),
        ("root_uuid", root_uuid.encode("ascii")),
        ("extra_args", extra_args.encode("ascii")),
    )
    for name, value in fields:
        encoded_name = name.encode("ascii")
        digest.update(len(encoded_name).to_bytes(2, "big"))
        digest.update(encoded_name)
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def stage_bundle(
    *,
    kernel_path: str | os.PathLike[str],
    initrd_path: str | os.PathLike[str],
    dtb_path: str | os.PathLike[str],
    boot_dir: str | os.PathLike[str],
    root_uuid: str,
    apply: bool = False,
    system_root: str | os.PathLike[str] = "/",
    running_release: str | None = None,
    mountinfo_path: str | os.PathLike[str] = "/proc/self/mountinfo",
    effective_uid: int | None = None,
) -> dict[str, object]:
    """Validate and stage a same-release bundle; activation is the final write."""
    current = _load_verified_bundle(Path(boot_dir))
    boot_path = current["boot_dir"]
    system_path = Path(system_root).resolve()
    image_marker = system_path / "etc/m17s-armbian/image"
    try:
        marker_mode = image_marker.lstat().st_mode
    except FileNotFoundError:
        marker_mode = 0
    if not stat.S_ISREG(marker_mode):
        raise ValueError("refusing to stage without /etc/m17s-armbian/image")
    fstype = _mount_fstype(boot_path, Path(mountinfo_path))
    if fstype not in {"vfat", "msdos", "fat"}:
        raise ValueError(f"boot directory must be on a FAT mount, found {fstype or 'none'}")

    kernel = _regular_bytes(Path(kernel_path), "candidate kernel")
    initrd = _regular_bytes(Path(initrd_path), "candidate uInitrd")
    dtb = _regular_bytes(Path(dtb_path), "candidate DTB")
    release = kernel_release(kernel)
    active_release = running_release or platform.uname().release
    if release != active_release:
        raise ValueError(
            f"kernel release change {active_release!r} -> {release!r} is not supported in v0.1"
        )
    modules_dir = system_path / "lib/modules" / release
    if not modules_dir.is_dir() or modules_dir.is_symlink():
        raise ValueError(f"installed modules directory is missing: /lib/modules/{release}")

    if kernel != current["kernel"]:
        raise ValueError(
            "candidate kernel bytes differ from the active kernel; kernel replacement is not supported in v0.1"
        )

    canonical_uuid = boot.validate_root_uuid(root_uuid)
    active_uuid = current["manifest"]["root_uuid"]
    if canonical_uuid != active_uuid:
        raise ValueError("root UUID changes are not supported by m17s-boot stage")
    extra_args = current["manifest"].get("extra_args", "")
    boot.validate_extra_args(extra_args)
    slot_name = f"{release}-{_slot_digest(kernel, initrd, dtb, canonical_uuid, extra_args)}"
    prefix = f"slots/{slot_name}"
    generated = boot.generate_boot_files(
        current["original_uboot"],
        canonical_uuid,
        kernel,
        initrd,
        dtb,
        extra_args,
        payload_prefix=prefix,
    )
    slot_dir = boot_path / "slots" / slot_name
    plan = {
        "status": "ready" if not apply else "applied",
        "apply": apply,
        "kernel_release": release,
        "slot": prefix,
        "slot_dir": str(slot_dir),
        "activation_path": str(boot_path / "emmc_autoscript"),
        "previous_path": str(boot_path / "previous-emmc_autoscript"),
    }
    if not apply:
        return plan
    uid = os.geteuid() if effective_uid is None else effective_uid
    if uid != 0:
        raise ValueError("--apply requires root privileges")
    slots_dir = boot_path / "slots"
    if slots_dir.is_symlink() or (slots_dir.exists() and not slots_dir.is_dir()):
        raise ValueError(f"slots path must be a real directory: {slots_dir}")
    if slot_dir.exists() or slot_dir.is_symlink():
        raise ValueError(f"refusing to overwrite existing slot: {slot_dir}")

    slot_files = {
        "Image": kernel,
        "uInitrd": initrd,
        boot.DTB_PATH.lstrip("/"): dtb,
        **generated,
    }
    slots_dir.mkdir(exist_ok=True)
    slot_dir.mkdir(exist_ok=False)
    for relative, data in sorted(slot_files.items()):
        _write_file(slot_dir / relative, data)
    os.sync()

    active_path = boot_path / "emmc_autoscript"
    previous_path = boot_path / "previous-emmc_autoscript"
    old_active = _regular_bytes(active_path, "active emmc_autoscript")
    _atomic_replace_bytes(previous_path, old_active)
    os.sync()
    _atomic_replace_bytes(active_path, generated["emmc_autoscript"])
    os.sync()
    try:
        verified = verify_bundle(boot_path)
        if verified["payload_prefix"] != prefix:
            raise ValueError("activated bundle verification selected the wrong slot")
    except (OSError, ValueError):
        _atomic_replace_bytes(active_path, old_active)
        os.sync()
        raise
    plan["verified"] = verified
    return plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m17s-boot")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="verify the active eMMC boot bundle")
    verify.add_argument("--boot-dir", default="/boot")
    stage = commands.add_parser("stage", help="validate and stage a same-release bundle")
    stage.add_argument("--kernel", required=True)
    stage.add_argument("--initrd", required=True)
    stage.add_argument("--dtb", required=True)
    stage.add_argument("--boot-dir", default="/boot")
    stage.add_argument("--root-uuid", required=True)
    stage.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_bundle(args.boot_dir)
        else:
            result = stage_bundle(
                kernel_path=args.kernel,
                initrd_path=args.initrd,
                dtb_path=args.dtb,
                boot_dir=args.boot_dir,
                root_uuid=args.root_uuid,
                apply=args.apply,
            )
    except (OSError, ValueError) as exc:
        print(f"m17s-boot: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
