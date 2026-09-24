import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from m17s_armbian import boot, maintenance


RELEASE = "6.12.109-ophub"
ROOT_UUID = "12345678-1234-5678-9abc-def012345678"


def image(release=RELEASE, size=4096):
    value = bytearray(size)
    struct.pack_into("<Q", value, 16, size)
    value[56:60] = b"ARM\x64"
    banner = f"Linux version {release} (builder@test)".encode()
    value[128 : 128 + len(banner)] = banner
    return bytes(value)


def uinitrd(data=b"gzip initrd payload"):
    fields = [
        0x27051956,
        0,
        0,
        len(data),
        0,
        0,
        zlib.crc32(data) & 0xFFFFFFFF,
        5,
        22,
        3,
        1,
        b"uInitrd".ljust(32, b"\0"),
    ]
    header = struct.pack(">7I4B32s", *fields)
    fields[1] = zlib.crc32(header) & 0xFFFFFFFF
    return struct.pack(">7I4B32s", *fields) + data


def dtb(size=128):
    value = bytearray(size)
    struct.pack_into(">II", value, 0, 0xD00DFEED, size)
    return bytes(value)


def synthetic_uboot():
    value = bytearray(boot.ORIGINAL_UBOOT_SIZE)
    value[1024 : 1024 + len(boot.OLD_BOOTCMD)] = boot.OLD_BOOTCMD
    return bytes(value)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.boot_dir = self.root / "boot"
        self.boot_dir.mkdir()
        (self.root / "etc/m17s-armbian").mkdir(parents=True)
        (self.root / "etc/m17s-armbian/image").write_text("m17s\n")
        (self.root / "lib/modules" / RELEASE).mkdir(parents=True)
        self.mountinfo = self.root / "mountinfo"
        self.mountinfo.write_text(
            f"36 25 0:32 / {self.boot_dir} rw,relatime - vfat /dev/test rw\n"
        )
        self.original = synthetic_uboot()
        self.hash_patch = mock.patch.object(
            boot, "_sha256", side_effect=self._sha256_with_synthetic_uboot
        )
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)
        self._install_initial_bundle()

    def _sha256_with_synthetic_uboot(self, value):
        if value == self.original:
            return boot.ORIGINAL_UBOOT_SHA256
        return hashlib.sha256(value).hexdigest()

    def _install_initial_bundle(self):
        kernel = image()
        initrd = uinitrd()
        device_tree = dtb()
        generated = boot.generate_boot_files(
            self.original, ROOT_UUID, kernel, initrd, device_tree, "quiet loglevel=4"
        )
        payloads = {
            "zImage": kernel,
            "uInitrd": initrd,
            boot.DTB_PATH.lstrip("/"): device_tree,
            **generated,
        }
        for relative, data in payloads.items():
            path = self.boot_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def _candidate_paths(
        self, release=RELEASE, *, kernel_data=None, initrd_payload=b"refreshed gzip initrd", dtb_size=256
    ):
        source = self.root / "source"
        source.mkdir(exist_ok=True)
        paths = (source / "Image", source / "uInitrd", source / "board.dtb")
        paths[0].write_bytes(image(release) if kernel_data is None else kernel_data)
        paths[1].write_bytes(uinitrd(initrd_payload))
        paths[2].write_bytes(dtb(dtb_size))
        return paths

    def stage(
        self, *, apply=False, release=RELEASE, uid=0, root_uuid=ROOT_UUID,
        kernel_data=None, initrd_payload=b"refreshed gzip initrd", dtb_size=256,
    ):
        kernel, initrd, device_tree = self._candidate_paths(
            release, kernel_data=kernel_data, initrd_payload=initrd_payload, dtb_size=dtb_size
        )
        return maintenance.stage_bundle(
            kernel_path=kernel,
            initrd_path=initrd,
            dtb_path=device_tree,
            boot_dir=self.boot_dir,
            root_uuid=root_uuid,
            apply=apply,
            system_root=self.root,
            running_release=RELEASE,
            mountinfo_path=self.mountinfo,
            effective_uid=uid,
        )

    def test_verify_checks_complete_active_bundle(self):
        result = maintenance.verify_bundle(self.boot_dir)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["payload_prefix"], "")
        self.assertEqual(result["kernel_release"], RELEASE)

    def test_verify_rejects_tampered_payload(self):
        path = self.boot_dir / "uInitrd"
        path.write_bytes(path.read_bytes() + b"tamper")
        with self.assertRaisesRegex(ValueError, "hash or size mismatch"):
            maintenance.verify_bundle(self.boot_dir)

    def test_dry_run_writes_nothing(self):
        before = sorted(str(path.relative_to(self.boot_dir)) for path in self.boot_dir.rglob("*"))
        old_active = (self.boot_dir / "emmc_autoscript").read_bytes()
        result = self.stage()
        after = sorted(str(path.relative_to(self.boot_dir)) for path in self.boot_dir.rglob("*"))
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["apply"])
        self.assertEqual(before, after)
        self.assertEqual((self.boot_dir / "emmc_autoscript").read_bytes(), old_active)

    def test_apply_writes_slot_then_activates_and_preserves_previous(self):
        old_active = (self.boot_dir / "emmc_autoscript").read_bytes()
        result = self.stage(apply=True)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            (self.boot_dir / "previous-emmc_autoscript").read_bytes(), old_active
        )
        self.assertNotEqual((self.boot_dir / "emmc_autoscript").read_bytes(), old_active)
        slot_dir = Path(result["slot_dir"])
        self.assertTrue((slot_dir / "Image").is_file())
        self.assertTrue((slot_dir / "uInitrd").is_file())
        self.assertTrue((slot_dir / boot.DTB_PATH.lstrip("/")).is_file())
        verified = maintenance.verify_bundle(self.boot_dir)
        self.assertEqual(verified["payload_prefix"], result["slot"])
        manifest = json.loads((slot_dir / "manifest.json").read_bytes())
        self.assertEqual(manifest["extra_args"], "quiet loglevel=4")

    def test_two_consecutive_initrd_refreshes_use_distinct_slots(self):
        first = self.stage(apply=True, initrd_payload=b"first refreshed initrd")
        first_active = (self.boot_dir / "emmc_autoscript").read_bytes()
        second = self.stage(apply=True, initrd_payload=b"second refreshed initrd")
        self.assertNotEqual(first["slot"], second["slot"])
        self.assertTrue(Path(first["slot_dir"]).is_dir())
        self.assertTrue(Path(second["slot_dir"]).is_dir())
        self.assertEqual(
            (self.boot_dir / "previous-emmc_autoscript").read_bytes(), first_active
        )
        self.assertEqual(maintenance.verify_bundle(self.boot_dir)["payload_prefix"], second["slot"])

    def test_root_uuid_change_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "root UUID changes"):
            self.stage(root_uuid="87654321-4321-6789-abcd-0123456789ab")

    def test_same_release_different_kernel_bytes_are_rejected(self):
        changed = bytearray(image())
        changed[300] ^= 1
        with self.assertRaisesRegex(ValueError, "kernel bytes differ"):
            self.stage(kernel_data=bytes(changed))

    def test_release_change_is_explicitly_rejected(self):
        with self.assertRaisesRegex(ValueError, "not supported in v0.1"):
            self.stage(release="6.13.0-unsupported")

    def test_missing_modules_image_marker_and_fat_are_rejected(self):
        (self.root / "lib/modules" / RELEASE).rmdir()
        with self.assertRaisesRegex(ValueError, "modules"):
            self.stage()
        (self.root / "lib/modules" / RELEASE).mkdir()
        (self.root / "etc/m17s-armbian/image").unlink()
        with self.assertRaisesRegex(ValueError, "m17s-armbian/image"):
            self.stage()
        (self.root / "etc/m17s-armbian/image").write_text("m17s\n")
        self.mountinfo.write_text(
            f"36 25 0:32 / {self.boot_dir} rw,relatime - ext4 /dev/test rw\n"
        )
        with self.assertRaisesRegex(ValueError, "FAT"):
            self.stage()

    def test_apply_requires_root_before_any_write(self):
        with self.assertRaisesRegex(ValueError, "root privileges"):
            self.stage(apply=True, uid=1000)
        self.assertFalse((self.boot_dir / "slots").exists())

    def test_failed_post_activation_verify_restores_old_entry(self):
        old_active = (self.boot_dir / "emmc_autoscript").read_bytes()
        with mock.patch.object(
            maintenance, "verify_bundle", side_effect=ValueError("simulated media failure")
        ):
            with self.assertRaisesRegex(ValueError, "simulated media failure"):
                self.stage(apply=True)
        self.assertEqual((self.boot_dir / "emmc_autoscript").read_bytes(), old_active)


if __name__ == "__main__":
    unittest.main()
