#!/usr/bin/env python3
"""Collect read-only M17S USB/eMMC acceptance evidence as public-safe JSON.

The file is self-contained so it can be sent to ``python3 -`` over an existing
SSH session.  It deliberately omits network addresses, MAC addresses, CID,
host keys, user names, and device serial/model fields.  Filesystem UUIDs are
represented by stable SHA-256 fingerprints rather than their raw values.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Sequence


PACKAGE_ROOT = Path("/usr/local/lib/m17s-armbian")
EMMC_DEVICE_RE = re.compile(r"^/dev/(mmcblk\d+)(?:p\d+)?$")
EMMC_PARTITION_RE = re.compile(r"^/dev/(mmcblk\d+)p(\d+)$")
SD_DISK_RE = re.compile(r"^/dev/sd[a-z]+$")
BLOCK_NAME_RE = re.compile(r"^[A-Za-z0-9._!+-]+$")


def command(arguments: Sequence[str], timeout: int = 20) -> dict[str, Any]:
    """Run a fixed argv-only, read-only probe and retain an auditable status."""
    try:
        result = subprocess.run(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr.strip(),
    }


def regular_text(path: str, *, max_bytes: int = 4096) -> dict[str, Any]:
    target = Path(path)
    try:
        mode = target.lstat().st_mode
        if not stat.S_ISREG(mode):
            return {"present": True, "valid": False, "error": "not a regular file"}
        data = target.read_bytes()
        if len(data) > max_bytes:
            return {"present": True, "valid": False, "error": "file is unexpectedly large"}
        return {
            "present": True,
            "valid": True,
            "value": data.decode("utf-8", errors="strict").strip(),
        }
    except FileNotFoundError:
        return {"present": False, "valid": False}
    except (OSError, UnicodeError) as exc:
        return {"present": True, "valid": False, "error": f"{type(exc).__name__}: {exc}"}


def uuid_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    digest = hashlib.sha256(value.strip().lower().encode("ascii", errors="strict")).hexdigest()
    return f"sha256:{digest}"


def redact_uuids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: uuid_fingerprint(item) if key.lower() == "uuid" and isinstance(item, str)
            else redact_uuids(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_uuids(item) for item in value]
    return value


def json_command(arguments: Sequence[str]) -> dict[str, Any]:
    result = command(arguments)
    if not result["ok"]:
        return result
    try:
        parsed = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid JSON: {exc}"}
    return {"ok": True, "data": redact_uuids(parsed)}


def find_mount(target: str) -> dict[str, Any]:
    return json_command(
        [
            "findmnt", "--json", "--target", target,
            "--output", "TARGET,SOURCE,FSTYPE,OPTIONS,UUID",
        ]
    )


def raw_findmnt_field(target: str, field: str) -> str | None:
    result = command(["findmnt", "--noheadings", "--raw", "--target", target, "--output", field])
    if not result["ok"]:
        return None
    value = result["stdout"].strip()
    return value or None


def block_inventory() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = command(
        [
            "lsblk", "--json", "--bytes",
            "--output", "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,PKNAME,RO,RM,TRAN",
        ]
    )
    if not result["ok"]:
        return result, []
    try:
        private = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid lsblk JSON: {exc}"}, []

    flat: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        flat.append(node)
        for child in node.get("children", []):
            visit(child)

    for device in private.get("blockdevices", []):
        visit(device)
    for item in flat:
        if item.get("type") != "part":
            continue
        kname = item.get("kname")
        parent_name = item.get("pkname")
        if (
            not isinstance(kname, str)
            or not isinstance(parent_name, str)
            or BLOCK_NAME_RE.fullmatch(kname) is None
            or BLOCK_NAME_RE.fullmatch(parent_name) is None
        ):
            item["sysfs_parent_valid"] = False
            continue
        try:
            child = (Path("/sys/class/block") / kname).resolve(strict=True)
            parent = (Path("/sys/class/block") / parent_name).resolve(strict=True)
            number_text = (child / "partition").read_text(encoding="ascii").strip()
            number = int(number_text)
            if number <= 0:
                raise ValueError("partition number is not positive")
            item["partn"] = number
            item["sysfs_parent_valid"] = child.parent == parent
        except (OSError, UnicodeError, ValueError):
            item["sysfs_parent_valid"] = False
    return {"ok": True, "data": redact_uuids(private)}, flat


def failed_units() -> dict[str, Any]:
    result = command(["systemctl", "--failed", "--all", "--no-legend", "--plain", "--no-pager"])
    if not result["ok"]:
        return result
    units = []
    for line in result["stdout"].splitlines():
        fields = line.split(None, 4)
        if len(fields) >= 4:
            units.append({"unit": fields[0], "load": fields[1], "active": fields[2], "sub": fields[3]})
    return {"ok": True, "count": len(units), "units": units}


def service_state(unit: str) -> dict[str, Any]:
    result = command(
        [
            "systemctl", "show", unit, "--no-pager",
            "--property=LoadState,ActiveState,SubState,UnitFileState",
        ]
    )
    if not result["ok"]:
        return result
    fields: dict[str, str] = {}
    for line in result["stdout"].splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    return {"ok": True, **fields}


def service_timing_state(unit: str) -> dict[str, Any]:
    properties = (
        "Id,ActiveState,SubState,Result,"
        "ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic"
    )
    result = command(
        ["systemctl", "show", unit, "--no-pager", f"--property={properties}"]
    )
    if not result["ok"]:
        return {
            "ok": False,
            "unit": unit,
            "error": result.get("error") or result.get("stderr") or "systemctl show failed",
            "returncode": result.get("returncode"),
        }
    fields: dict[str, str] = {}
    for line in result["stdout"].splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    return {"ok": True, **fields}


def evaluate_console_setup_ordering(
    tmpfiles: dict[str, Any], console_setup: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate boot ordering from systemd monotonic timestamps only."""
    field_names = {
        "Id",
        "ActiveState",
        "SubState",
        "Result",
        "ExecMainStartTimestampMonotonic",
        "ExecMainExitTimestampMonotonic",
    }
    missing = {
        "systemd-tmpfiles-setup.service": sorted(
            name for name in field_names if name not in tmpfiles or tmpfiles.get(name) == ""
        ),
        "console-setup.service": sorted(
            name for name in field_names if name not in console_setup or console_setup.get(name) == ""
        ),
    }
    if not tmpfiles.get("ok") or not console_setup.get("ok") or any(missing.values()):
        return {
            "ok": False,
            "error": "systemd timing query failed or omitted required fields",
            "missing_fields": missing,
            "systemd_tmpfiles_setup": tmpfiles,
            "console_setup": console_setup,
        }
    try:
        tmpfiles_exit = int(tmpfiles["ExecMainExitTimestampMonotonic"])
        console_start = int(console_setup["ExecMainStartTimestampMonotonic"])
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": "systemd monotonic timestamp is not an integer",
            "missing_fields": missing,
            "systemd_tmpfiles_setup": tmpfiles,
            "console_setup": console_setup,
        }
    checks = {
        "tmpfiles_unit_id_exact": tmpfiles["Id"] == "systemd-tmpfiles-setup.service",
        "console_unit_id_exact": console_setup["Id"] == "console-setup.service",
        "tmpfiles_result_success": tmpfiles["Result"] == "success",
        "console_result_success": console_setup["Result"] == "success",
        "tmpfiles_exit_is_positive": tmpfiles_exit > 0,
        "console_start_is_positive": console_start > 0,
        "tmpfiles_exited_before_console_started": 0 < tmpfiles_exit <= console_start,
        "console_oneshot_is_active": console_setup["ActiveState"] == "active",
        "console_oneshot_substate_is_exited": console_setup["SubState"] == "exited",
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "systemd_tmpfiles_setup": tmpfiles,
        "console_setup": console_setup,
        "ordering": {
            "tmpfiles_exit_monotonic_usec": tmpfiles_exit,
            "console_start_monotonic_usec": console_start,
        },
    }


def drm_state() -> dict[str, Any]:
    connectors: list[dict[str, Any]] = []
    for connector in sorted(Path("/sys/class/drm").glob("card*-*")):
        if not connector.is_dir():
            continue
        item: dict[str, Any] = {"connector": connector.name}
        for name in ("status", "enabled", "dpms"):
            probe = regular_text(str(connector / name))
            if probe.get("valid"):
                item[name] = probe["value"]
        modes = regular_text(str(connector / "modes"), max_bytes=65536)
        if modes.get("valid"):
            item["modes"] = [line for line in modes["value"].splitlines() if line]
        connectors.append(item)
    connected = [item["connector"] for item in connectors if item.get("status") == "connected"]
    return {
        "connectors": connectors,
        "connected_connectors": connected,
        "system_reports_connected": bool(connected),
        "requires_user_screen_confirmation": True,
        "note": "DRM sysfs only reports kernel-side state; visible HDMI output requires user confirmation.",
    }


def emmc_parent(source: str | None) -> str | None:
    if not source:
        return None
    # findmnt may append a btrfs-style [subvolume] suffix; this image does not,
    # but removing it keeps device classification conservative.
    device = source.split("[", 1)[0]
    match = EMMC_DEVICE_RE.fullmatch(device)
    return match.group(1) if match else None


def emmc_partition(source: str | None) -> tuple[str, int] | None:
    if not source:
        return None
    match = EMMC_PARTITION_RE.fullmatch(source.split("[", 1)[0])
    return (match.group(1), int(match.group(2))) if match else None


def usb_partition_evidence(
    source: str | None,
    *,
    expected_number: int,
    expected_fstype: str,
    flat_blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind a mount source to one exact partition of an enumerated USB disk."""
    if not source:
        return {"ok": False, "error": "mount source is unavailable"}
    device = source.split("[", 1)[0]
    matches = [
        item for item in flat_blocks
        if item.get("type") == "part" and item.get("path") == device
    ]
    if len(matches) != 1:
        return {"ok": False, "error": "mount source is not one enumerated partition"}
    partition = matches[0]
    try:
        number = int(partition.get("partn"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "enumerated partition number is unavailable"}
    parent_name = partition.get("pkname")
    parents = [
        item for item in flat_blocks
        if item.get("type") == "disk" and item.get("kname") == parent_name
    ]
    if len(parents) != 1:
        return {"ok": False, "error": "partition does not have one enumerated parent disk"}
    parent = parents[0]
    checks = {
        "partition_number_matches": number == expected_number,
        "partition_fstype_matches": partition.get("fstype") == expected_fstype,
        "partition_sysfs_parent_matches": partition.get("sysfs_parent_valid") is True,
        "parent_transport_is_usb": parent.get("tran") == "usb",
    }
    return {
        "ok": all(checks.values()),
        "device": device,
        "parent": parent.get("path"),
        "partition_number": number,
        "fstype": partition.get("fstype"),
        "parent_transport": parent.get("tran"),
        "checks": checks,
    }


def verify_usb_uuid(actual_root_uuid: str | None) -> dict[str, Any]:
    try:
        sys.path.insert(0, str(PACKAGE_ROOT))
        from m17s_armbian import build  # type: ignore

        return {
            "ok": bool(actual_root_uuid),
            "matches_release_usb_uuid": bool(
                actual_root_uuid and actual_root_uuid.lower() == build.USB_UUID.lower()
            ),
        }
    except Exception as exc:
        return {"ok": False, "matches_release_usb_uuid": False,
                "error": f"{type(exc).__name__}: {exc}"}


def verify_emmc_bundle(actual_root_uuid: str | None) -> dict[str, Any]:
    try:
        sys.path.insert(0, str(PACKAGE_ROOT))
        from m17s_armbian import build, maintenance  # type: ignore

        verified = maintenance.verify_bundle("/boot")
        bundle_uuid = verified.pop("root_uuid", None)
        verified["root_uuid_fingerprint"] = uuid_fingerprint(bundle_uuid)
        verified["root_uuid_matches_mounted_root"] = bool(
            actual_root_uuid and bundle_uuid and actual_root_uuid.lower() == bundle_uuid.lower()
        )
        verified["root_uuid_differs_from_emmc_template"] = bool(
            actual_root_uuid and actual_root_uuid.lower() != build.EMMC_UUID.lower()
        )
        return {"ok": True, **verified}
    except Exception as exc:  # verification failure must remain structured evidence
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    image = regular_text("/etc/m17s-armbian/image")
    media_marker = regular_text("/etc/m17s-armbian/media")
    provisioned = regular_text("/etc/m17s-armbian/provisioned")
    media = media_marker.get("value") if media_marker.get("valid") else None

    root_source = raw_findmnt_field("/", "SOURCE")
    boot_source = raw_findmnt_field("/boot", "SOURCE")
    root_uuid = raw_findmnt_field("/", "UUID")
    root_fstype = raw_findmnt_field("/", "FSTYPE")
    boot_fstype = raw_findmnt_field("/boot", "FSTYPE")
    root_parent = emmc_parent(root_source)
    boot_parent = emmc_parent(boot_source)
    root_partition = emmc_partition(root_source)
    boot_partition = emmc_partition(boot_source)
    root_mount = find_mount("/")
    boot_mount = find_mount("/boot")
    blocks, flat_blocks = block_inventory()
    usb_storage = sorted(
        str(item.get("path"))
        for item in flat_blocks
        if item.get("type") == "disk"
        and (item.get("tran") == "usb" or SD_DISK_RE.fullmatch(str(item.get("path", ""))))
    )

    failed = failed_units()
    ssh_service = service_state("ssh.service")
    ssh_socket = service_state("ssh.socket")
    ssh_active = any(
        state.get("ActiveState") == "active" for state in (ssh_service, ssh_socket)
    )
    console_setup_ordering = evaluate_console_setup_ordering(
        service_timing_state("systemd-tmpfiles-setup.service"),
        service_timing_state("console-setup.service"),
    )

    bundle: dict[str, Any] = {"applicable": media == "emmc"}
    if media == "emmc":
        bundle.update(verify_emmc_bundle(root_uuid))

    common_checks = {
        "image_marker_present": bool(image.get("valid") and image.get("value")),
        "media_marker_valid": media in {"usb", "emmc"},
        "provisioned_marker_present": bool(provisioned.get("valid")),
        "failed_service_count_zero": failed.get("ok") is True and failed.get("count") == 0,
        "ssh_active": ssh_active,
        "console_setup_runs_after_tmpfiles": console_setup_ordering.get("ok") is True,
    }
    if media == "usb":
        usb_root = usb_partition_evidence(
            root_source,
            expected_number=2,
            expected_fstype="ext4",
            flat_blocks=flat_blocks,
        )
        usb_boot = usb_partition_evidence(
            boot_source,
            expected_number=1,
            expected_fstype="vfat",
            flat_blocks=flat_blocks,
        )
        usb_uuid = verify_usb_uuid(root_uuid)
        usb_layout = {"root": usb_root, "boot": usb_boot, "root_uuid": usb_uuid}
        media_checks = {
            "findmnt_collection_succeeded": root_mount.get("ok") is True and boot_mount.get("ok") is True,
            "lsblk_collection_succeeded": blocks.get("ok") is True,
            "usb_root_is_partition_2_ext4": usb_root.get("ok") is True and root_fstype == "ext4",
            "usb_boot_is_partition_1_vfat": usb_boot.get("ok") is True and boot_fstype == "vfat",
            "usb_root_and_boot_share_disk": bool(
                usb_root.get("parent") and usb_root.get("parent") == usb_boot.get("parent")
            ),
            "usb_root_uuid_matches_release": usb_uuid.get("matches_release_usb_uuid") is True,
        }
    elif media == "emmc":
        usb_layout = {"applicable": False}
        media_checks = {
            "root_and_boot_are_emmc": root_parent is not None and boot_parent is not None,
            "root_and_boot_share_emmc": root_parent is not None and root_parent == boot_parent,
            "root_is_emmc_partition_2_ext4": bool(root_partition and root_partition[1] == 2 and root_fstype == "ext4"),
            "boot_is_emmc_partition_1_vfat": bool(boot_partition and boot_partition[1] == 1 and boot_fstype == "vfat"),
            "no_usb_storage_present": not usb_storage,
            "boot_bundle_verified": bundle.get("ok") is True,
            "bundle_uuid_matches_root": bundle.get("root_uuid_matches_mounted_root") is True,
            "root_uuid_changed_from_template": bundle.get("root_uuid_differs_from_emmc_template") is True,
        }
    else:
        usb_layout = {"applicable": False}
        media_checks = {"recognized_boot_media": False}

    checks = {**common_checks, **media_checks}
    report = {
        "format": 1,
        "privacy": {
            "public_safe": True,
            "omitted": ["IP addresses", "MAC addresses", "eMMC CID", "device serial/model", "private keys", "SSH host keys", "user names"],
            "uuid_policy": "raw UUIDs omitted; stable SHA-256 fingerprints are reported",
        },
        "boot_id": regular_text("/proc/sys/kernel/random/boot_id"),
        "uname": {
            "sysname": os.uname().sysname,
            "release": os.uname().release,
            "version": os.uname().version,
            "machine": os.uname().machine,
        },
        "markers": {
            "image": image,
            "media": media_marker,
            "provisioned": {"present": provisioned.get("valid") is True},
        },
        "classification": media if media in {"usb", "emmc"} else "unknown",
        "mounts": {"root": root_mount, "boot": boot_mount},
        "root_uuid_fingerprint": uuid_fingerprint(root_uuid),
        "block_devices": blocks,
        "usb_storage_devices": usb_storage,
        "failed_units": failed,
        "ssh": {"service": ssh_service, "socket": ssh_socket, "active": ssh_active},
        "console_setup_ordering": console_setup_ordering,
        "boot_bundle": bundle,
        "usb_layout": usb_layout,
        "hdmi": drm_state(),
        "acceptance": {
            "checks": checks,
            "system_checks_pass": all(checks.values()),
            "hdmi_screen_confirmation_required": True,
            "scope": "System checks are read-only. HDMI acceptance additionally requires the user to confirm visible output.",
        },
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
