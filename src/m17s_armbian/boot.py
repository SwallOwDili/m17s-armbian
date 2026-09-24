"""Build the verified M17S two-stage RAM boot files entirely in memory."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re
import struct
import uuid
import zlib


ORIGINAL_UBOOT_SIZE = 606_670
ORIGINAL_UBOOT_SHA256 = (
    "c3b2065356e61cec05320e68010135a315e7d89d0e6d6dd212a55a28cf90f7e8"
)
OLD_BOOTCMD = b"bootcmd=run distro_bootcmd\0"
NEW_BOOTCMD = b"bootcmd=source 0x021000000\0"

UBOOT_ADDR = 0x01000000
DTB_ADDR = 0x08008000
KERNEL_ADDR = 0x08080000
INITRD_ADDR = 0x13000000
SCRIPT_ADDR = 0x21000000
SCRIPT_SLOT_END = 0x22000000

DTB_PATH = "/dtb/amlogic/meson-gxl-s905x-p212.dtb"

_UIMAGE_MAGIC = 0x27051956
_FDT_MAGIC = 0xD00DFEED
_ARM64_IMAGE_MAGIC = b"ARM\x64"
_UIMAGE_HEADER = struct.Struct(">7I4B32s")
_SAFE_ARG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._,:/@%+=-]*\Z")
_RESERVED_ARG_KEYS = {
    "root",
    "loadaddr",
    "kernel_addr",
    "kernel_addr_r",
    "ramdisk_addr",
    "ramdisk_addr_r",
    "fdt_addr",
    "fdt_addr_r",
    "scriptaddr",
}
_PATH_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _bytes(value: bytes, label: str) -> bytes:
    if not isinstance(value, bytes):
        raise ValueError(f"{label} must be bytes")
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_original_uboot(original_uboot: bytes) -> int:
    """Validate and return the unique bootcmd entry offset."""
    original_uboot = _bytes(original_uboot, "original U-Boot")
    if len(original_uboot) != ORIGINAL_UBOOT_SIZE:
        raise ValueError(
            f"original U-Boot size must be {ORIGINAL_UBOOT_SIZE} bytes"
        )
    if _sha256(original_uboot) != ORIGINAL_UBOOT_SHA256:
        raise ValueError("original U-Boot SHA-256 does not match the verified P212 binary")
    if len(OLD_BOOTCMD) != len(NEW_BOOTCMD):
        raise ValueError("internal bootcmd replacement length mismatch")
    if original_uboot.count(OLD_BOOTCMD) != 1:
        raise ValueError("verified bootcmd entry is not unique")
    return original_uboot.index(OLD_BOOTCMD)


def patch_uboot(original_uboot: bytes) -> tuple[bytes, int]:
    """Apply the sole verified, equal-length bootcmd change."""
    offset = validate_original_uboot(original_uboot)
    patched = original_uboot[:offset] + NEW_BOOTCMD + original_uboot[offset + len(OLD_BOOTCMD) :]
    if len(patched) != len(original_uboot):
        raise ValueError("patched U-Boot length changed")
    old_nuls = [index for index, byte in enumerate(original_uboot) if byte == 0]
    new_nuls = [index for index, byte in enumerate(patched) if byte == 0]
    if old_nuls != new_nuls:
        raise ValueError("patched U-Boot NUL positions changed")
    changed = [
        index
        for index, (old, new) in enumerate(zip(original_uboot, patched))
        if old != new
    ]
    value_start = offset + len(b"bootcmd=")
    value_end = offset + len(OLD_BOOTCMD) - 1
    if not changed or min(changed) < value_start or max(changed) >= value_end:
        raise ValueError("patched U-Boot changed bytes outside the bootcmd value")
    return patched, offset


def validate_root_uuid(root_uuid: str) -> str:
    if not isinstance(root_uuid, str):
        raise ValueError("root UUID must be a string")
    try:
        parsed = uuid.UUID(root_uuid)
    except (ValueError, AttributeError) as exc:
        raise ValueError("root UUID must be a canonical UUID") from exc
    canonical = str(parsed)
    if root_uuid.lower() != canonical:
        raise ValueError("root UUID must use the canonical hyphenated form")
    return canonical


def validate_extra_args(extra_args: str) -> list[str]:
    if not isinstance(extra_args, str):
        raise ValueError("extra_args must be a string")
    if not extra_args:
        return []
    if extra_args != extra_args.strip():
        raise ValueError("extra_args must not have leading or trailing whitespace")
    words = extra_args.split(" ")
    if any(not word for word in words):
        raise ValueError("extra_args must use single spaces between words")
    for word in words:
        if _SAFE_ARG.fullmatch(word) is None:
            raise ValueError(f"unsafe kernel argument: {word!r}")
        key = word.split("=", 1)[0].lower()
        if key in _RESERVED_ARG_KEYS or key.endswith("_addr") or key.endswith("_addr_r"):
            raise ValueError(f"kernel argument may not override {key}")
    return words


def validate_payload_prefix(payload_prefix: str) -> str:
    """Validate a U-Boot/FAT relative directory without normalizing it."""
    if not isinstance(payload_prefix, str):
        raise ValueError("payload_prefix must be a string")
    if payload_prefix == "":
        return ""
    if payload_prefix in {".", ".."}:
        raise ValueError("payload_prefix must name a relative directory")
    if len(payload_prefix) > 160:
        raise ValueError("payload_prefix is too long")
    path = PurePosixPath(payload_prefix)
    if path.is_absolute() or str(path) != payload_prefix:
        raise ValueError("payload_prefix must be a normalized relative path")
    if any(
        component in {"", ".", ".."} or _PATH_COMPONENT.fullmatch(component) is None
        for component in path.parts
    ):
        raise ValueError("payload_prefix contains an unsafe path component")
    return payload_prefix


def _payload_path(payload_prefix: str, relative_path: str) -> str:
    if payload_prefix:
        return f"/{payload_prefix}/{relative_path}"
    return f"/{relative_path}"


def validate_arm64_image(kernel: bytes) -> int:
    kernel = _bytes(kernel, "kernel")
    if len(kernel) <= 64:
        raise ValueError("ARM64 Image must contain data beyond its 64-byte header")
    if kernel[56:60] != _ARM64_IMAGE_MAGIC:
        raise ValueError("kernel does not have the ARM64 Image magic")
    declared_size = struct.unpack_from("<Q", kernel, 16)[0]
    if declared_size == 0:
        raise ValueError("ARM64 Image declares a zero image size")
    if declared_size < len(kernel):
        raise ValueError("ARM64 Image declared size is smaller than the file size")
    return declared_size


def validate_uinitrd(initrd: bytes) -> int:
    initrd = _bytes(initrd, "uInitrd")
    if len(initrd) < _UIMAGE_HEADER.size:
        raise ValueError("uInitrd is shorter than its 64-byte legacy header")
    fields = _UIMAGE_HEADER.unpack_from(initrd)
    magic, header_crc, _timestamp, data_size, _load, _entry, data_crc = fields[:7]
    os_id, arch_id, type_id, compression_id = fields[7:11]
    if magic != _UIMAGE_MAGIC:
        raise ValueError("uInitrd has an invalid legacy image magic")
    header = bytearray(initrd[: _UIMAGE_HEADER.size])
    header[4:8] = b"\0\0\0\0"
    if (zlib.crc32(header) & 0xFFFFFFFF) != header_crc:
        raise ValueError("uInitrd legacy header CRC mismatch")
    if data_size != len(initrd) - _UIMAGE_HEADER.size:
        raise ValueError("uInitrd declared data size does not match the file size")
    data = initrd[_UIMAGE_HEADER.size :]
    if (zlib.crc32(data) & 0xFFFFFFFF) != data_crc:
        raise ValueError("uInitrd data CRC mismatch")
    if os_id != 5:
        raise ValueError("uInitrd must identify Linux as its operating system")
    if arch_id != 22:
        raise ValueError("uInitrd must identify the AArch64 architecture")
    if type_id != 3:
        raise ValueError("uInitrd must be a legacy RAMDisk image")
    if compression_id != 1:
        raise ValueError("uInitrd must identify gzip compression")
    return data_size


def validate_dtb(dtb: bytes) -> int:
    dtb = _bytes(dtb, "DTB")
    if len(dtb) < 40:
        raise ValueError("DTB is shorter than its fixed header")
    magic, total_size = struct.unpack_from(">II", dtb)
    if magic != _FDT_MAGIC:
        raise ValueError("DTB has an invalid FDT magic")
    if total_size != len(dtb):
        raise ValueError("DTB total size does not match the file size")
    return total_size


def _check_load_regions(
    patched_uboot: bytes,
    kernel: bytes,
    kernel_image_size: int,
    initrd: bytes,
    dtb: bytes,
    stage2_script: bytes,
) -> None:
    regions = [
        ("U-Boot", UBOOT_ADDR, len(patched_uboot), DTB_ADDR),
        ("DTB", DTB_ADDR, len(dtb), KERNEL_ADDR),
        ("kernel", KERNEL_ADDR, max(len(kernel), kernel_image_size), INITRD_ADDR),
        ("uInitrd", INITRD_ADDR, len(initrd), SCRIPT_ADDR),
        ("stage-two script", SCRIPT_ADDR, len(stage2_script), SCRIPT_SLOT_END),
    ]
    for label, start, size, limit in regions:
        if size <= 0:
            raise ValueError(f"{label} payload is empty")
        if start + size > limit:
            raise ValueError(
                f"{label} payload exceeds its reserved RAM region ending at 0x{limit:08x}"
            )


def _legacy_script(command: bytes, name: str) -> bytes:
    if not isinstance(command, bytes):
        raise ValueError("script command must be bytes")
    try:
        encoded_name = name.encode("ascii")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise ValueError("script name must be ASCII") from exc
    if len(encoded_name) > 32:
        raise ValueError("script name is longer than 32 bytes")
    data = struct.pack(">II", len(command), 0) + command
    fields = [
        _UIMAGE_MAGIC,
        0,
        0,  # deterministic timestamp
        len(data),
        0,
        0,
        zlib.crc32(data) & 0xFFFFFFFF,
        5,  # Linux
        2,  # ARM, matching the verified script image
        6,  # Script
        0,  # no compression
        encoded_name.ljust(32, b"\0"),
    ]
    header = _UIMAGE_HEADER.pack(*fields)
    fields[1] = zlib.crc32(header) & 0xFFFFFFFF
    return _UIMAGE_HEADER.pack(*fields) + data


def generate_boot_files(
    original_uboot: bytes,
    root_uuid: str,
    kernel: bytes,
    initrd: bytes,
    dtb: bytes,
    extra_args: str = "",
    *,
    payload_prefix: str = "",
) -> dict[str, bytes]:
    """Validate all inputs and return five boot files plus ``manifest.json``."""
    patched_uboot, bootcmd_offset = patch_uboot(original_uboot)
    canonical_uuid = validate_root_uuid(root_uuid)
    extra_words = validate_extra_args(extra_args)
    prefix = validate_payload_prefix(payload_prefix)
    kernel_image_size = validate_arm64_image(kernel)
    validate_uinitrd(initrd)
    validate_dtb(dtb)

    base_args = [
        f"root=UUID={canonical_uuid}",
        "rootflags=data=writeback",
        "rw",
        "rootfstype=ext4",
        "console=ttyAML0,115200n8",
        "console=tty0",
        "no_console_suspend",
        "consoleblank=0",
        "fsck.fix=yes",
        "fsck.repair=yes",
        "net.ifnames=0",
        "max_loop=128",
        "cgroup_enable=cpuset",
        "cgroup_memory=1",
        "cgroup_enable=memory",
        "swapaccount=1",
        "video=HDMI-A-1:1920x1080@60e",
        "plymouth.enable=0",
        "rootwait",
    ]
    bootargs = " ".join(base_args + extra_words)
    stage2_text = (
        'echo "M17S RAM handoff: mainline boots preloaded eMMC payloads"\n'
        f"if fdt addr 0x{DTB_ADDR:08x}; then\n"
        f"    setenv bootargs '{bootargs}'\n"
        "    if printenv ethaddr; then setenv bootargs ${bootargs} mac=${ethaddr}; fi\n"
        f"    booti 0x{KERNEL_ADDR:08x} 0x{INITRD_ADDR:08x} 0x{DTB_ADDR:08x}\n"
        "fi\n"
        'echo "M17S RAM handoff failed. Reinsert the known-good USB disk and power cycle."\n'
    )
    stage1_text = (
        'echo "M17S eMMC preload: vendor U-Boot reads all payloads"\n'
        f"if fatload mmc 1:1 0x{KERNEL_ADDR:08x} {_payload_path(prefix, 'Image' if prefix else 'zImage')}; then\n"
        f"    if fatload mmc 1:1 0x{INITRD_ADDR:08x} {_payload_path(prefix, 'uInitrd')}; then\n"
        f"        if fatload mmc 1:1 0x{DTB_ADDR:08x} {_payload_path(prefix, DTB_PATH.lstrip('/'))}; then\n"
        f"            if fatload mmc 1:1 0x{SCRIPT_ADDR:08x} {_payload_path(prefix, 'm17s-ramboot.scr')}; then\n"
        f"                if fatload mmc 1:1 0x{UBOOT_ADDR:08x} {_payload_path(prefix, 'u-boot-m17s-ram.bin')}; then go 0x{UBOOT_ADDR:08x}; fi\n"
        "            fi\n"
        "        fi\n"
        "    fi\n"
        "fi\n"
    )
    try:
        stage1_command = stage1_text.encode("ascii")
        stage2_command = stage2_text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("generated U-Boot scripts must be ASCII") from exc
    stage1_image = _legacy_script(stage1_command, "emmc_autoscript")
    stage2_image = _legacy_script(stage2_command, "m17s-ramboot")
    _check_load_regions(
        patched_uboot, kernel, kernel_image_size, initrd, dtb, stage2_image
    )

    files = {
        "u-boot-m17s-ram.bin": patched_uboot,
        "emmc_autoscript.cmd": stage1_command,
        "emmc_autoscript": stage1_image,
        "m17s-ramboot.cmd": stage2_command,
        "m17s-ramboot.scr": stage2_image,
    }
    inputs = {
        "kernel": {
            "bytes": len(kernel),
            "image_size": kernel_image_size,
            "sha256": _sha256(kernel),
        },
        "uInitrd": {"bytes": len(initrd), "sha256": _sha256(initrd)},
        DTB_PATH: {"bytes": len(dtb), "sha256": _sha256(dtb)},
    }
    outputs = {
        name: {"bytes": len(data), "sha256": _sha256(data)}
        for name, data in sorted(files.items())
    }
    manifest = {
        "format": 1,
        "original_uboot": {
            "bytes": len(original_uboot),
            "sha256": ORIGINAL_UBOOT_SHA256,
        },
        "patched_bootcmd_offset": bootcmd_offset,
        "root_uuid": canonical_uuid,
        "payload_prefix": prefix,
        "extra_args": " ".join(extra_words),
        "addresses": {
            "uboot": f"0x{UBOOT_ADDR:08x}",
            "dtb": f"0x{DTB_ADDR:08x}",
            "kernel": f"0x{KERNEL_ADDR:08x}",
            "initrd": f"0x{INITRD_ADDR:08x}",
            "ram_script": f"0x{SCRIPT_ADDR:08x}",
        },
        "inputs": inputs,
        "outputs": outputs,
    }
    files["manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("ascii")
    return files
