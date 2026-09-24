import base64
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from m17s_armbian import installer


def public_key():
    kind = b"ssh-ed25519"
    blob = len(kind).to_bytes(4, "big") + kind + (32).to_bytes(4, "big") + b"x" * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def artifact(name, raw_size, *, file=None):
    return installer.Artifact(name, file or f"{name}.img.gz", "a" * 64, 100,
                              "b" * 64, raw_size)


def probe(*, partitions=True, mbr=None, product=installer.EMMC_PRODUCT,
          root_disk="/dev/sda", cid="15010038574d453452123456789abcde",
          mounts=()):
    total_sectors = installer.EMMC_BYTES // installer.SECTOR_SIZE
    parts = ()
    if partitions:
        parts = (
            installer.Partition(1, "/dev/mmcblk2p1", installer.BOOT_START_SECTOR,
                                installer.BOOT_SECTORS),
            installer.Partition(2, "/dev/mmcblk2p2", installer.ROOT_START_SECTOR,
                                total_sectors - installer.ROOT_START_SECTOR),
        )
    return installer.DeviceProbe(
        path="/dev/mmcblk2", major_minor="179:0", sysfs_path="/sys/devices/mmcblk2",
        cid=cid, product=product, size_bytes=installer.EMMC_BYTES,
        sector_size=installer.SECTOR_SIZE, removable=False, device_type="MMC",
        compatible=("amlogic,p212", "amlogic,meson-gxl"),
        ram_bytes=installer.PROFILE_RAM_BYTES,
        boot0="/dev/mmcblk2boot0", boot1="/dev/mmcblk2boot1",
        boot0_bytes=4 * 1024 * 1024, boot1_bytes=4 * 1024 * 1024,
        partitions=parts, root_disk=root_disk, mounts=tuple(mounts),
        mbr_table_bytes=mbr if mbr is not None else
            installer._mbr_partition_bytes(total_sectors).hex(),
    )


def release(directory="/release"):
    return installer.Release(directory, "0.1.0", "c" * 64,
                             artifact("emmc_boot", installer.BOOT_IMAGE_BYTES),
                             artifact("emmc_root", installer.ROOT_IMAGE_BYTES))


class FakeExecutor:
    def __init__(self, device, mount_root):
        self.device = device
        self.probe_sequence = []
        self.mount_root = Path(mount_root)
        self.events = []
        self.after_initialize = None
        self.boot_readonly_mutations = []

    def probe(self, target):
        self.events.append(("probe", target))
        if self.probe_sequence:
            return self.probe_sequence.pop(0)
        return self.device

    def verify_backup_entry(self, directory, entry):
        self.events.append(("verify-backup", entry["source"]))

    def backing_disk(self, path):
        return "/dev/sda"

    def free_bytes(self, path):
        return 16 * 1024 * 1024 * 1024

    def hash_range(self, path, offset, length):
        self.events.append(("hash", path, offset, length))
        return hashlib.sha256(f"{path}:{offset}:{length}".encode()).hexdigest()

    def write_at(self, path, offset, data):
        self.events.append(("write-at", path, offset, data))

    def reread_partitions(self, path):
        self.events.append(("reread", path))
        if self.after_initialize is not None:
            self.device = self.after_initialize

    def write_image(self, source, target, raw_size, raw_sha256):
        self.events.append(("write-image", target, raw_size, raw_sha256))

    def prepare_ext4(self, path):
        self.events.append(("prepare-ext4", path))
        return "12345678-1234-5678-9abc-def012345678"

    def require_fat_label(self, path, expected):
        self.events.append(("fat-label", path, expected))

    @contextlib.contextmanager
    def mounted(self, device, *, readonly=False):
        which = "root" if device.endswith("p2") else "boot"
        root = self.mount_root / which
        root.mkdir(parents=True, exist_ok=True)
        self.events.append(("mount", device, readonly))
        if readonly and which == "boot" and self.boot_readonly_mutations:
            name, data = self.boot_readonly_mutations.pop(0)
            (root / name).write_bytes(data)
        yield root
        self.events.append(("unmount", device, readonly))


def write_backup_manifest(path, target):
    entries = []
    for source, size in ((target.path, target.size_bytes),
                         (target.boot0, target.boot0_bytes),
                         (target.boot1, target.boot1_bytes)):
        entries.append({"file": Path(source).name + ".gz", "source": source,
                        "raw_size": size, "raw_sha256": "1" * 64,
                        "size": 1, "sha256": "2" * 64})
    value = {"schema_version": 1, "board": installer.BOARD, "cid": target.cid,
             "target_size": target.size_bytes,
             "target_identity": installer._probe_identity(target), "entries": entries}
    path.write_text(json.dumps(value))


def fake_materializer(_release, _artifact, destination):
    destination.write_bytes(b"fixture")


def fake_generator(**kwargs):
    assert kwargs["root_uuid"] == "12345678-1234-5678-9abc-def012345678"
    return {
        "u-boot-m17s-ram.bin": b"patched",
        "emmc_autoscript.cmd": b"stage1 text",
        "emmc_autoscript": b"ACTIVE",
        "m17s-ramboot.cmd": b"stage2 text",
        "m17s-ramboot.scr": b"stage2",
        "manifest.json": b"{}\n",
    }


class InstallerTests(unittest.TestCase):
    def test_load_release_accepts_builder_metadata_without_usb_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)

            def metadata(name, raw_size):
                payload = f"compressed fixture for {name}".encode()
                path = directory / f"{name}.img.gz"
                path.write_bytes(payload)
                return {
                    "file": path.name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                    "raw_sha256": hashlib.sha256(f"raw {name}".encode()).hexdigest(),
                    "raw_size": raw_size,
                }

            manifest = {
                "schema_version": 1,
                "version": "0.1.0rc1",
                "board": installer.BOARD,
                "status": "candidate-not-hardware-tested",
                "kernel_release": "6.12.0-current-meson64",
                "artifacts": {
                    "emmc_boot": metadata("emmc_boot", installer.BOOT_IMAGE_BYTES),
                    "emmc_root": metadata("emmc_root", installer.ROOT_IMAGE_BYTES),
                    "usb": {
                        "file": "m17s-armbian-usb.img.gz",
                        "sha256": "1" * 64,
                        "size": 123,
                        "raw_sha256": "2" * 64,
                        "raw_size": 456,
                    },
                },
                "sources": {"debian": {"url": "https://example.invalid/source"}},
                "source_files": {"installer.py": "3" * 64},
                "privacy": {"authorized_keys": "absent"},
                "reproducibility": "pinned inputs and recipe",
                "signature": "unsigned local candidate",
            }
            (directory / "release.json").write_text(json.dumps(manifest))

            loaded = installer.load_release(directory)

            self.assertEqual(loaded.version, "0.1.0rc1")
            self.assertEqual(loaded.boot_artifact.raw_size, installer.BOOT_IMAGE_BYTES)
            self.assertEqual(loaded.root_artifact.raw_size, installer.ROOT_IMAGE_BYTES)
            self.assertFalse((directory / "m17s-armbian-usb.img.gz").exists())

    def test_load_release_rejects_unknown_manifest_or_artifact_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            base = {
                "schema_version": 1,
                "version": "test",
                "board": installer.BOARD,
                "artifacts": {
                    "emmc_boot": {},
                    "emmc_root": {},
                },
            }
            for field, value, message in (
                ("unexpected", True, "unknown fields"),
                (None, None, "unknown artifacts"),
            ):
                manifest = json.loads(json.dumps(base))
                if field is None:
                    manifest["artifacts"]["other"] = {}
                else:
                    manifest[field] = value
                (directory / "release.json").write_text(json.dumps(manifest))
                with self.subTest(message=message):
                    with self.assertRaisesRegex(installer.InstallerError, message):
                        installer.load_release(directory)

    def test_existing_layout_is_exact_and_unknown_layout_is_rejected(self):
        installer._validate_layout(probe(), False)
        bad = probe()
        bad = installer.dataclasses.replace(
            bad, partitions=(installer.dataclasses.replace(bad.partitions[0], start_sector=1),
                             bad.partitions[1]))
        with self.assertRaisesRegex(installer.InstallerError, "partition 1"):
            installer._validate_layout(bad, False)
        with self.assertRaisesRegex(installer.InstallerError, "no existing partitions"):
            installer._validate_layout(probe(), True)

    def test_existing_layout_requires_consistent_mbr_and_rejects_gpt(self):
        total = installer.EMMC_BYTES // installer.SECTOR_SIZE
        good = bytearray(installer._mbr_partition_bytes(total))
        cases = []
        bad_signature = bytearray(good)
        bad_signature[64:66] = b"\0\0"
        cases.append((bad_signature, "55aa"))
        bad_type = bytearray(good)
        bad_type[4] = 0x06
        cases.append((bad_type, "type/LBA"))
        bad_lba = bytearray(good)
        bad_lba[8:12] = (installer.BOOT_START_SECTOR + 1).to_bytes(4, "little")
        cases.append((bad_lba, "type/LBA"))
        extra = bytearray(good)
        extra[32 + 4] = 0x83
        cases.append((extra, "other than p1 and p2"))
        for table, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(installer.InstallerError, message):
                    installer._validate_layout(probe(mbr=bytes(table).hex()), False)
        with self.assertRaisesRegex(installer.InstallerError, "GPT"):
            installer._validate_layout(
                installer.dataclasses.replace(probe(), gpt_signature=True), False
            )

    def test_fresh_layout_requires_empty_mbr_and_no_gpt(self):
        fresh = probe(partitions=False, mbr=(b"\0" * 66).hex())
        installer._validate_layout(fresh, True)
        with self.assertRaisesRegex(installer.InstallerError, "not all zero"):
            installer._validate_layout(probe(partitions=False, mbr="01" + "00" * 65), True)
        with self.assertRaisesRegex(installer.InstallerError, "GPT"):
            installer._validate_layout(installer.dataclasses.replace(fresh, gpt_signature=True), True)

    def test_hardware_root_mount_swap_and_holder_gates(self):
        cases = [
            (installer.dataclasses.replace(probe(), product="other"), "product"),
            (installer.dataclasses.replace(probe(), root_disk="/dev/mmcblk2"), "root/USB"),
            (installer.dataclasses.replace(probe(), swap_devices=("/dev/mmcblk2p2",)), "swap"),
            (installer.dataclasses.replace(probe(), holders=("dm-0",)), "holder"),
            (installer.dataclasses.replace(probe(), mounts=("/media/emmc",)), "whole target"),
            (installer.dataclasses.replace(
                probe(), partitions=(installer.dataclasses.replace(probe().partitions[0], mounts=("/boot",)),
                                     probe().partitions[1])), "mounted"),
        ]
        for value, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(installer.InstallerError, message):
                    installer._validate_layout(value, False)

    def test_mbr_initializer_changes_only_table_and_signature(self):
        table = installer._mbr_partition_bytes(installer.EMMC_BYTES // 512)
        self.assertEqual(len(table), 66)
        self.assertEqual(table[64:], b"\x55\xaa")
        first = table[:16]
        second = table[16:32]
        self.assertEqual(first[4], 0x0C)
        self.assertEqual(int.from_bytes(first[8:12], "little"), installer.BOOT_START_SECTOR)
        self.assertEqual(int.from_bytes(first[12:16], "little"), installer.BOOT_SECTORS)
        self.assertEqual(second[4], 0x83)
        self.assertEqual(int.from_bytes(second[8:12], "little"), installer.ROOT_START_SECTOR)
        self.assertEqual(table[32:64], b"\0" * 32)

    def test_partition_probe_detects_primary_and_backup_gpt(self):
        sector = installer.SECTOR_SIZE
        size = sector * 8
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "disk.img"
            path.write_bytes(b"\0" * size)
            table, has_gpt = installer._read_partition_table_state(str(path), size, sector)
            self.assertEqual(table, "00" * 66)
            self.assertFalse(has_gpt)

            with path.open("r+b") as handle:
                handle.seek(sector)
                handle.write(b"EFI PART")
            self.assertTrue(installer._read_partition_table_state(str(path), size, sector)[1])

            path.write_bytes(b"\0" * size)
            with path.open("r+b") as handle:
                handle.seek(size - sector)
                handle.write(b"EFI PART")
            self.assertTrue(installer._read_partition_table_state(str(path), size, sector)[1])

    def test_artifact_extraction_checks_both_hashes_and_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            raw = b"abc" * 1000
            compressed = directory / "root.img.gz"
            with compressed.open("wb") as raw_handle:
                with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as handle:
                    handle.write(raw)
            compressed_hash, compressed_size = installer._sha256_file(compressed)
            item = installer.Artifact("emmc_root", compressed.name, compressed_hash,
                                      compressed_size, hashlib.sha256(raw).hexdigest(), len(raw))
            rel = installer.Release(str(directory), "test", "f" * 64,
                                    artifact("emmc_boot", 1), item)
            output = directory / "root.img"
            installer._materialize_artifact(rel, item, output)
            self.assertEqual(output.read_bytes(), raw)
            output.unlink()
            bad = installer.dataclasses.replace(item, raw_sha256="0" * 64)
            with self.assertRaisesRegex(installer.InstallerError, "raw artifact"):
                installer._materialize_artifact(rel, bad, output)

    def test_fstab_rewrite_removes_old_root_and_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc").mkdir()
            (root / "etc/fstab").write_text(
                "UUID=old / ext4 defaults 0 1\nLABEL=BOOT /boot vfat defaults 0 2\n"
                "tmpfs /tmp tmpfs defaults 0 0\n"
            )
            uuid = "12345678-1234-5678-9abc-def012345678"
            installer._rewrite_fstab(root, uuid)
            result = (root / "etc/fstab").read_text()
            self.assertIn(f"UUID={uuid} / ext4", result)
            self.assertIn("LABEL=BOOT_EMMC /boot vfat", result)
            self.assertIn("tmpfs /tmp", result)
            self.assertNotIn("UUID=old", result)

    def test_uenv_rewrite_is_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "uEnv.txt"
            path.write_text("LINUX=/zImage\nAPPEND=root=UUID=old rw rootwait\n")
            uuid = "12345678-1234-5678-9abc-def012345678"
            installer._rewrite_uenv(root, uuid)
            self.assertEqual(
                path.read_text(),
                f"LINUX=/zImage\nAPPEND=root=UUID={uuid} rw rootwait\n",
            )

    def test_apply_writes_root_then_boot_and_activates_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = probe()
            fake = FakeExecutor(device, root / "mounts")
            (root / "mounts/root/etc").mkdir(parents=True)
            (root / "mounts/root/etc/fstab").write_text("UUID=old / ext4 defaults 0 1\n")
            (root / "mounts/root/etc/m17s-armbian").mkdir(parents=True)
            boot_root = root / "mounts/boot"
            (boot_root / installer.DTB_RELATIVE.parent).mkdir(parents=True)
            (boot_root / "uEnv.txt").write_text("APPEND=root=UUID=old rw rootwait\n")
            for path in (boot_root / "u-boot-p212.bin", boot_root / "zImage",
                         boot_root / "uInitrd", boot_root / installer.DTB_RELATIVE):
                path.write_bytes(b"input")
            rel = release()
            plan = installer.create_plan(rel, device, initialize_layout=False)
            backup = root / "backup.json"
            write_backup_manifest(backup, device)
            result = installer.apply_plan(
                plan, rel, backup, fake,
                confirmation=f"ERASE {device.path} {device.cid}",
                work_dir=root,
                materializer=fake_materializer, generator=fake_generator,
            )
            self.assertEqual(result["status"], "installed")
            writes = [event for event in fake.events if event[0] == "write-image"]
            self.assertEqual([event[1] for event in writes], ["/dev/mmcblk2p2", "/dev/mmcblk2p1"])
            self.assertEqual((boot_root / "emmc_autoscript").read_bytes(), b"ACTIVE")
            # A read-only remount occurs after the activation write and verifies its bytes.
            self.assertIn(("mount", "/dev/mmcblk2p1", True), fake.events)

    def test_apply_verifies_firstboot_bytes_before_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = probe()
            fake = FakeExecutor(device, root / "mounts")
            (root / "mounts/root/etc").mkdir(parents=True)
            (root / "mounts/root/etc/fstab").write_text("UUID=old / ext4 defaults 0 1\n")
            (root / "mounts/root/etc/m17s-armbian").mkdir(parents=True)
            boot_root = root / "mounts/boot"
            (boot_root / installer.DTB_RELATIVE.parent).mkdir(parents=True)
            (boot_root / "uEnv.txt").write_text("APPEND=root=UUID=old rw rootwait\n")
            for path in (
                boot_root / "u-boot-p212.bin",
                boot_root / "zImage",
                boot_root / "uInitrd",
                boot_root / installer.DTB_RELATIVE,
            ):
                path.write_bytes(b"input")
            config = root / "firstboot.json"
            config.write_text(json.dumps({
                "hostname": "m17s-headless",
                "username": "m17s",
                "ssh_authorized_keys": [public_key()],
            }))
            rel = release()
            plan = installer.create_plan(rel, device, initialize_layout=False)
            backup = root / "backup.json"
            write_backup_manifest(backup, device)
            fake.boot_readonly_mutations.append(("firstboot.json", b"tampered\n"))

            with self.assertRaisesRegex(installer.InstallerError, "firstboot.json"):
                installer.apply_plan(
                    plan,
                    rel,
                    backup,
                    fake,
                    confirmation=f"ERASE {device.path} {device.cid}",
                    work_dir=root,
                    firstboot_config=config,
                    materializer=fake_materializer,
                    generator=fake_generator,
                )
            self.assertFalse((boot_root / "emmc_autoscript").exists())

    def test_firstboot_config_requires_public_key_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = probe()
            rel = release()
            plan = installer.create_plan(rel, device, initialize_layout=False)
            backup = root / "backup.json"
            write_backup_manifest(backup, device)
            config = root / "firstboot.json"
            config.write_text(json.dumps({"hostname": "m17s-headless"}))
            fake = FakeExecutor(device, root / "mounts")
            with self.assertRaisesRegex(installer.InstallerError, "SSH public key"):
                installer.apply_plan(
                    plan, rel, backup, fake,
                    confirmation=f"ERASE {device.path} {device.cid}", work_dir=root,
                    firstboot_config=config, materializer=fake_materializer,
                    generator=fake_generator,
                )
            self.assertFalse(any(event[0].startswith("write") for event in fake.events))

    def test_apply_refuses_wrong_confirmation_and_changed_device_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = probe()
            rel = release()
            plan = installer.create_plan(rel, device, initialize_layout=False)
            backup = root / "backup.json"
            write_backup_manifest(backup, device)
            fake = FakeExecutor(device, root / "mounts")
            with self.assertRaisesRegex(installer.InstallerError, "confirmation"):
                installer.apply_plan(plan, rel, backup, fake, confirmation="wrong",
                                     work_dir=root,
                                     materializer=fake_materializer, generator=fake_generator)
            self.assertFalse(any(event[0].startswith("write") for event in fake.events))

    def test_apply_rechecks_runtime_state_after_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = probe()
            rel = release()
            plan = installer.create_plan(rel, device, initialize_layout=False)
            backup = root / "backup.json"
            write_backup_manifest(backup, device)
            fake = FakeExecutor(device, root / "mounts")
            fake.probe_sequence = [
                device,
                installer.dataclasses.replace(device, mounts=("/late-automount",)),
            ]
            with self.assertRaisesRegex(installer.InstallerError, "identity changed|mounted"):
                installer.apply_plan(
                    plan, rel, backup, fake,
                    confirmation=f"ERASE {device.path} {device.cid}", work_dir=root,
                    materializer=fake_materializer, generator=fake_generator,
                )
            self.assertFalse(any(event[0].startswith("write") for event in fake.events))

            changed = installer.dataclasses.replace(device, cid="0" * 32)
            fake = FakeExecutor(changed, root / "mounts2")
            with self.assertRaisesRegex(installer.InstallerError, "identity changed"):
                installer.apply_plan(plan, rel, backup, fake,
                                     confirmation=f"ERASE {device.path} {device.cid}",
                                     work_dir=root,
                                     materializer=fake_materializer, generator=fake_generator)
            self.assertFalse(any(event[0].startswith("write") for event in fake.events))

    def test_initialize_writes_only_mbr_bytes_446_through_511(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fresh = probe(partitions=False, mbr=(b"\0" * 66).hex())
            existing = probe()
            fake = FakeExecutor(fresh, root / "mounts")
            fake.after_initialize = existing
            (root / "mounts/root/etc").mkdir(parents=True)
            (root / "mounts/root/etc/fstab").write_text("UUID=old / ext4 defaults 0 1\n")
            (root / "mounts/root/etc/m17s-armbian").mkdir(parents=True)
            boot_root = root / "mounts/boot"
            (boot_root / installer.DTB_RELATIVE.parent).mkdir(parents=True)
            (boot_root / "uEnv.txt").write_text("APPEND=root=UUID=old rw rootwait\n")
            for path in (boot_root / "u-boot-p212.bin", boot_root / "zImage",
                         boot_root / "uInitrd", boot_root / installer.DTB_RELATIVE):
                path.write_bytes(b"input")
            rel = release()
            plan = installer.create_plan(rel, fresh, initialize_layout=True)
            backup = root / "backup.json"
            write_backup_manifest(backup, fresh)
            installer.apply_plan(
                plan, rel, backup, fake,
                confirmation=f"ERASE {fresh.path} {fresh.cid}",
                work_dir=root,
                materializer=fake_materializer, generator=fake_generator,
            )
            writes = [event for event in fake.events if event[0] == "write-at"]
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0][2], 446)
            self.assertEqual(len(writes[0][3]), 66)


if __name__ == "__main__":
    unittest.main()
