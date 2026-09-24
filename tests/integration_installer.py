"""Privileged Linux loop integration for the eMMC installer.

This module is intentionally outside unittest's default ``test*.py`` pattern.
Run it only in the pinned privileged builder container after release artifacts
exist::

    M17S_RUN_LOOP_INTEGRATION=1 \
    M17S_RELEASE_DIR=/absolute/path/to/release \
      python -m unittest tests.integration_installer -v

Hardware discovery is synthetic. Filesystem writes, fsck/resize/UUID changes,
mounts, complete gzip backup verification, protected-range hashes, boot-file
generation, and final activation use the production implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from m17s_armbian import build, installer, maintenance


REQUIRED_TOOLS = (
    "losetup",
    "blockdev",
    "e2fsck",
    "resize2fs",
    "tune2fs",
    "blkid",
    "mount",
    "umount",
    "findmnt",
    "sync",
    "fallocate",
)
SYNTHETIC_CID = "15010038574d453452123456789abcde"


def _command(argv: list[str]) -> str:
    result = subprocess.run(argv, check=False, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise RuntimeError(
            f"integration setup command failed ({result.returncode}): {argv!r}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


class LoopExecutor(installer.SystemExecutor):
    """Inject only the unavailable hardware facts; retain real block I/O."""

    def __init__(self, probe: installer.DeviceProbe) -> None:
        self.synthetic_probe = probe

    def probe(self, target: str) -> installer.DeviceProbe:
        if os.path.realpath(target) != os.path.realpath(self.synthetic_probe.path):
            raise installer.InstallerError("integration executor received an unexpected target")
        return self.synthetic_probe

    def backing_disk(self, path: Path) -> str | None:
        # The test has already confined every path to its private temporary tree.
        return None

    def run(self, argv, *, ok=(0,)):
        if list(argv) == ["udevadm", "settle"]:
            # Minimal builder containers commonly have no running udev daemon.
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        return super().run(argv, ok=ok)

    def write_image(self, source: Path, target: str, raw_size: int,
                    raw_sha256: str) -> None:
        # Payload images contain large zero ranges. Punch only those holes in the
        # ordinary temporary source; logical bytes and SHA-256 remain unchanged.
        # The production implementation still performs the actual device write
        # and both its source and target readback verification.
        self.run(["fallocate", "--dig-holes", str(source)])
        super().write_image(source, target, raw_size, raw_sha256)


class InstallerLoopIntegration(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        reasons = []
        if sys.platform != "linux":
            reasons.append("Linux is required")
        if os.geteuid() != 0:
            reasons.append("root is required for loop mounts")
        if os.environ.get("M17S_RUN_LOOP_INTEGRATION") != "1":
            reasons.append("M17S_RUN_LOOP_INTEGRATION=1 is required")
        release_value = os.environ.get("M17S_RELEASE_DIR")
        if not release_value:
            reasons.append("M17S_RELEASE_DIR is required")
        missing = [name for name in REQUIRED_TOOLS if shutil.which(name) is None]
        if missing:
            reasons.append("missing tools: " + ", ".join(missing))
        if reasons:
            self.skipTest("; ".join(reasons))

        self.release_dir = Path(release_value).resolve()
        if not (self.release_dir / "release.json").is_file():
            self.skipTest("M17S_RELEASE_DIR has no release.json")
        self.release = installer.load_release(self.release_dir)
        self.temporary = tempfile.TemporaryDirectory(prefix="m17s-loop-integration-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # Peak use is the hole-punched payload sources (~2.2 GiB), the written
        # loop backing file (~4.5 GiB), and a compact zero-heavy backup. Keep a
        # conservative 7 GiB floor so the test cannot consume the host volume.
        if shutil.disk_usage(self.root).free < 7 * 1024 * 1024 * 1024:
            self.skipTest("integration work filesystem has less than 7 GiB free")

        self.disk_image = self.root / "emmc-user.img"
        with self.disk_image.open("wb") as handle:
            handle.truncate(installer.EMMC_BYTES)
        # Sparse zero ranges keep the mandatory complete backup compact. Random
        # sentinels ensure preservation is not merely a zero-fill coincidence.
        self.sentinels = {
            4096: secrets.token_bytes(4096),
            64 * 1024 * 1024: secrets.token_bytes(4096),
            installer.PREFIX_BYTES - 8192: secrets.token_bytes(4096),
            (installer.BOOT_START_SECTOR + installer.BOOT_SECTORS) * 512 + 4096:
                secrets.token_bytes(4096),
        }
        with self.disk_image.open("r+b", buffering=0) as handle:
            for offset, value in self.sentinels.items():
                handle.seek(offset)
                handle.write(value)
            handle.seek(446)
            handle.write(installer._mbr_partition_bytes(installer.EMMC_BYTES // 512))
            handle.flush()
            os.fsync(handle.fileno())

        self.boot0_file = self.root / "boot0.img"
        self.boot1_file = self.root / "boot1.img"
        for index, path in enumerate((self.boot0_file, self.boot1_file)):
            with path.open("wb") as handle:
                handle.truncate(4 * 1024 * 1024)
                handle.seek(4096 + index * 4096)
                handle.write(secrets.token_bytes(4096))

        self.loops: list[str] = []
        self.addCleanup(self._detach_all)
        self.disk_loop = self._attach(self.disk_image)
        self.boot_loop = self._attach(
            self.disk_image,
            offset=installer.BOOT_START_SECTOR * 512,
            size=installer.BOOT_SECTORS * 512,
        )
        root_bytes = installer.EMMC_BYTES - installer.ROOT_START_SECTOR * 512
        self.root_loop = self._attach(
            self.disk_image,
            offset=installer.ROOT_START_SECTOR * 512,
            size=root_bytes,
        )
        total_sectors = installer.EMMC_BYTES // 512
        self.probe = installer.DeviceProbe(
            path=self.disk_loop,
            major_minor=self._major_minor(self.disk_loop),
            sysfs_path=f"/sys/class/block/{Path(self.disk_loop).name}",
            cid=SYNTHETIC_CID,
            product=installer.EMMC_PRODUCT,
            size_bytes=installer.EMMC_BYTES,
            sector_size=installer.SECTOR_SIZE,
            removable=False,
            device_type="MMC",
            compatible=(installer.PROFILE_COMPATIBLE, "amlogic,meson-gxl"),
            ram_bytes=installer.PROFILE_RAM_BYTES,
            boot0=str(self.boot0_file),
            boot1=str(self.boot1_file),
            boot0_bytes=self.boot0_file.stat().st_size,
            boot1_bytes=self.boot1_file.stat().st_size,
            partitions=(
                installer.Partition(1, self.boot_loop, installer.BOOT_START_SECTOR,
                                    installer.BOOT_SECTORS),
                installer.Partition(2, self.root_loop, installer.ROOT_START_SECTOR,
                                    total_sectors - installer.ROOT_START_SECTOR),
            ),
            root_disk="/dev/integration-usb",
            mbr_table_bytes=installer._mbr_partition_bytes(total_sectors).hex(),
        )
        self.executor = LoopExecutor(self.probe)

    def _attach(self, path: Path, *, offset: int | None = None,
                size: int | None = None) -> str:
        argv = ["losetup", "--find", "--show"]
        if offset is not None:
            argv.extend(["--offset", str(offset)])
        if size is not None:
            argv.extend(["--sizelimit", str(size)])
        argv.append(str(path))
        loop = _command(argv)
        self.loops.append(loop)
        return loop

    @staticmethod
    def _major_minor(device: str) -> str:
        value = os.stat(device).st_rdev
        return f"{os.major(value)}:{os.minor(value)}"

    def _detach_all(self) -> None:
        for device in reversed(self.loops):
            subprocess.run(["losetup", "--detach", device], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.loops.clear()

    def test_existing_layout_full_backup_apply_and_verify(self) -> None:
        print("installer integration: preparing plan and preservation hashes", flush=True)
        plan = installer.create_plan(self.release, self.probe, initialize_layout=False)
        plan_path = self.root / "install-plan.json"
        installer.save_plan(plan_path, plan)
        plan = installer.load_plan(plan_path)

        prefix_before = self.executor.hash_range(self.disk_loop, 0, installer.PREFIX_BYTES)
        gap_offset = (installer.BOOT_START_SECTOR + installer.BOOT_SECTORS) * 512
        gap_size = (installer.ROOT_START_SECTOR - installer.BOOT_START_SECTOR
                    - installer.BOOT_SECTORS) * 512
        gap_before = self.executor.hash_range(self.disk_loop, gap_offset, gap_size)
        boot0_before = self.executor.hash_range(str(self.boot0_file), 0,
                                                self.boot0_file.stat().st_size)
        boot1_before = self.executor.hash_range(str(self.boot1_file), 0,
                                                self.boot1_file.stat().st_size)

        backup_dir = self.root / "complete-backup"
        print("installer integration: creating and verifying complete backup", flush=True)
        backup = installer.create_backup(plan, backup_dir, self.executor)
        self.assertEqual([entry["raw_size"] for entry in backup["entries"]], [
            installer.EMMC_BYTES, 4 * 1024 * 1024, 4 * 1024 * 1024,
        ])
        print("installer integration: applying root and boot images", flush=True)
        result = installer.apply_plan(
            plan,
            self.release,
            backup_dir / "backup.json",
            self.executor,
            confirmation=f"ERASE {self.disk_loop} {SYNTHETIC_CID}",
            work_dir=self.root,
        )
        self.assertEqual(result["status"], "installed")
        self.assertRegex(result["root_uuid"], installer.UUID_RE)
        self.assertNotEqual(result["root_uuid"], build.EMMC_UUID)

        filesystem = _command(["tune2fs", "-l", self.root_loop])
        fields = {}
        for line in filesystem.splitlines():
            if ":" in line:
                name, value = line.split(":", 1)
                fields[name.strip()] = value.strip()
        block_count = int(fields["Block count"])
        block_size = int(fields["Block size"])
        root_partition_bytes = (
            installer.EMMC_BYTES
            - installer.ROOT_START_SECTOR * installer.SECTOR_SIZE
        )
        self.assertEqual(
            block_count * block_size,
            root_partition_bytes // block_size * block_size,
        )

        print("installer integration: verifying preserved ranges and installed files", flush=True)
        self.assertEqual(
            self.executor.hash_range(self.disk_loop, 0, installer.PREFIX_BYTES),
            prefix_before,
        )
        self.assertEqual(self.executor.hash_range(self.disk_loop, gap_offset, gap_size), gap_before)
        self.assertEqual(
            self.executor.hash_range(str(self.boot0_file), 0, self.boot0_file.stat().st_size),
            boot0_before,
        )
        self.assertEqual(
            self.executor.hash_range(str(self.boot1_file), 0, self.boot1_file.stat().st_size),
            boot1_before,
        )
        with self.disk_image.open("rb", buffering=0) as handle:
            for offset, expected in self.sentinels.items():
                handle.seek(offset)
                self.assertEqual(handle.read(len(expected)), expected)

        with self.executor.mounted(self.root_loop, readonly=True) as root_mount:
            fstab = (root_mount / "etc/fstab").read_text(encoding="utf-8")
            self.assertIn(f"UUID={result['root_uuid']} / ext4 ", fstab)
            self.assertIn("LABEL=BOOT_EMMC /boot vfat ", fstab)
            self.assertEqual(
                (root_mount / "etc/m17s-armbian/media").read_text(encoding="ascii"),
                "emmc\n",
            )
        with self.executor.mounted(self.boot_loop, readonly=True) as boot_mount:
            verified = maintenance.verify_bundle(boot_mount)
            self.assertEqual(verified["status"], "ok")
            self.assertEqual(verified["payload_prefix"], "")
            self.assertEqual(verified["root_uuid"], result["root_uuid"])
            self.assertTrue((boot_mount / "emmc_autoscript").is_file())
            self.assertTrue((boot_mount / "manifest.json").is_file())
            uenv = (boot_mount / "uEnv.txt").read_text(encoding="utf-8")
            self.assertIn(f"root=UUID={result['root_uuid']}", uenv)
        print("installer integration: verification complete", flush=True)


if __name__ == "__main__":
    unittest.main()
