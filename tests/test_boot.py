import hashlib
import json
from pathlib import Path
import struct
import sys
import unittest
from unittest import mock
import zlib


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from m17s_armbian import boot


def arm64_image(size=4096, *, declared_size=None):
    image = bytearray(size)
    struct.pack_into("<Q", image, 16, size if declared_size is None else declared_size)
    image[56:60] = b"ARM\x64"
    return bytes(image)


def legacy_uinitrd(data=b"synthetic gzip payload", *, arch=22, image_type=3, compression=1):
    fields = [
        0x27051956,
        0,
        0,
        len(data),
        0,
        0,
        zlib.crc32(data) & 0xFFFFFFFF,
        5,
        arch,
        image_type,
        compression,
        b"uInitrd".ljust(32, b"\0"),
    ]
    header = struct.pack(">7I4B32s", *fields)
    fields[1] = zlib.crc32(header) & 0xFFFFFFFF
    return struct.pack(">7I4B32s", *fields) + data


def dtb(size=128):
    blob = bytearray(size)
    struct.pack_into(">II", blob, 0, 0xD00DFEED, size)
    return bytes(blob)


def accepted_uboot():
    data = bytearray(boot.ORIGINAL_UBOOT_SIZE)
    data[1234 : 1234 + len(boot.OLD_BOOTCMD)] = boot.OLD_BOOTCMD
    return bytes(data)


class BootFilesTests(unittest.TestCase):
    def setUp(self):
        self.uboot = accepted_uboot()
        self.uuid = "12345678-1234-5678-9abc-def012345678"
        self.hash_patch = mock.patch.object(
            boot, "_sha256", side_effect=self._sha256_with_synthetic_uboot
        )
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)

    def _sha256_with_synthetic_uboot(self, value):
        if value == self.uboot:
            return boot.ORIGINAL_UBOOT_SHA256
        return hashlib.sha256(value).hexdigest()

    def generate(self, **changes):
        args = {
            "original_uboot": self.uboot,
            "root_uuid": self.uuid,
            "kernel": arm64_image(),
            "initrd": legacy_uinitrd(),
            "dtb": dtb(),
        }
        args.update(changes)
        return boot.generate_boot_files(**args)

    def test_generate_returns_five_files_and_manifest(self):
        files = self.generate(extra_args="quiet loglevel=4")
        self.assertEqual(
            set(files),
            {
                "u-boot-m17s-ram.bin",
                "emmc_autoscript.cmd",
                "emmc_autoscript",
                "m17s-ramboot.cmd",
                "m17s-ramboot.scr",
                "manifest.json",
            },
        )
        stage2 = files["m17s-ramboot.cmd"].decode("ascii")
        self.assertIn(f"root=UUID={self.uuid}", stage2)
        self.assertIn("quiet loglevel=4", stage2)
        self.assertIn("booti 0x08080000 0x13000000 0x08008000", stage2)
        stage1 = files["emmc_autoscript.cmd"].decode("ascii")
        self.assertIn(boot.DTB_PATH, stage1)
        self.assertNotIn("env import", stage1)
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest["root_uuid"], self.uuid)
        self.assertEqual(manifest["inputs"]["kernel"]["bytes"], 4096)
        self.assertEqual(manifest["inputs"]["kernel"]["image_size"], 4096)

    def test_payload_prefix_routes_every_stage_one_payload(self):
        files = self.generate(payload_prefix="slots/next-1")
        stage1 = files["emmc_autoscript.cmd"].decode("ascii")
        for relative in (
            "Image",
            "uInitrd",
            "dtb/amlogic/meson-gxl-s905x-p212.dtb",
            "m17s-ramboot.scr",
            "u-boot-m17s-ram.bin",
        ):
            self.assertIn(f"/slots/next-1/{relative}", stage1)
        self.assertEqual(json.loads(files["manifest.json"])["payload_prefix"], "slots/next-1")

    def test_unsafe_payload_prefix_is_rejected(self):
        for value in ("/slots/a", "slots/../a", "slots//a", "slots/a b", "slots/a;reset", "."):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.generate(payload_prefix=value)

    def test_script_headers_are_deterministic_and_valid(self):
        first = self.generate()
        second = self.generate()
        self.assertEqual(first, second)
        for name in ("emmc_autoscript", "m17s-ramboot.scr"):
            image = first[name]
            fields = struct.unpack(">7I4B32s", image[:64])
            self.assertEqual(fields[0], 0x27051956)
            self.assertEqual(fields[2], 0)
            self.assertEqual(fields[7:11], (5, 2, 6, 0))
            header = bytearray(image[:64])
            header[4:8] = b"\0" * 4
            self.assertEqual(zlib.crc32(header) & 0xFFFFFFFF, fields[1])
            self.assertEqual(zlib.crc32(image[64:]) & 0xFFFFFFFF, fields[6])
            command_size, zero = struct.unpack(">II", image[64:72])
            self.assertEqual(zero, 0)
            command_name = "emmc_autoscript.cmd" if name == "emmc_autoscript" else "m17s-ramboot.cmd"
            self.assertEqual(image[72:], first[command_name])
            self.assertEqual(command_size, len(image[72:]))

    def test_patch_changes_only_bootcmd_value_and_preserves_nuls(self):
        patched, offset = boot.patch_uboot(self.uboot)
        self.assertEqual(offset, 1234)
        self.assertEqual(patched[offset : offset + len(boot.NEW_BOOTCMD)], boot.NEW_BOOTCMD)
        self.assertEqual(len(patched), len(self.uboot))
        self.assertEqual(
            [i for i, byte in enumerate(patched) if byte == 0],
            [i for i, byte in enumerate(self.uboot) if byte == 0],
        )

    def test_bad_uboot_hash_is_rejected(self):
        with mock.patch.object(boot, "_sha256", return_value="0" * 64):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                boot.patch_uboot(self.uboot)

    def test_bad_arm64_header_and_declared_size_are_rejected(self):
        bad_magic = bytearray(arm64_image())
        bad_magic[56:60] = b"NOPE"
        with self.assertRaisesRegex(ValueError, "magic"):
            self.generate(kernel=bytes(bad_magic))
        bad_size = arm64_image(declared_size=4095)
        with self.assertRaisesRegex(ValueError, "declared size"):
            self.generate(kernel=bad_size)

    def test_arm64_declared_memory_image_may_exceed_file(self):
        kernel = arm64_image(declared_size=8192)
        files = self.generate(kernel=kernel)
        metadata = json.loads(files["manifest.json"])["inputs"]["kernel"]
        self.assertEqual(metadata["bytes"], 4096)
        self.assertEqual(metadata["image_size"], 8192)

    def test_arm64_declared_memory_image_must_fit_reserved_region(self):
        reserved = boot.INITRD_ADDR - boot.KERNEL_ADDR
        kernel = arm64_image(declared_size=reserved + 1)
        with self.assertRaisesRegex(ValueError, "reserved RAM region"):
            self.generate(kernel=kernel)

    def test_uinitrd_header_crc_is_rejected(self):
        bad = bytearray(legacy_uinitrd())
        bad[63] ^= 1
        with self.assertRaisesRegex(ValueError, "header CRC"):
            self.generate(initrd=bytes(bad))

    def test_uinitrd_data_crc_is_rejected(self):
        bad = bytearray(legacy_uinitrd())
        bad[-1] ^= 1
        with self.assertRaisesRegex(ValueError, "data CRC"):
            self.generate(initrd=bytes(bad))

    def test_uinitrd_type_arch_and_compression_are_rejected(self):
        cases = [
            (legacy_uinitrd(arch=2), "AArch64"),
            (legacy_uinitrd(image_type=2), "RAMDisk"),
            (legacy_uinitrd(compression=0), "gzip"),
        ]
        for value, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.generate(initrd=value)

    def test_invalid_uuid_is_rejected(self):
        for value in ("not-a-uuid", "12345678123456789abcdef012345678"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "canonical"):
                    self.generate(root_uuid=value)

    def test_shell_injection_and_reserved_args_are_rejected(self):
        values = [
            "quiet;reset",
            "quiet\nreset",
            "foo='bar'",
            "foo=$bar",
            "root=/dev/mmcblk0p2",
            "kernel_addr_r=0x1",
            "fdt_addr=0x2",
            "two  spaces",
        ]
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.generate(extra_args=value)

    def test_bad_dtb_magic_and_size_are_rejected(self):
        bad_magic = bytearray(dtb())
        bad_magic[0] ^= 1
        with self.assertRaisesRegex(ValueError, "magic"):
            self.generate(dtb=bytes(bad_magic))
        bad_size = bytearray(dtb())
        struct.pack_into(">I", bad_size, 4, len(bad_size) - 1)
        with self.assertRaisesRegex(ValueError, "total size"):
            self.generate(dtb=bytes(bad_size))

    def test_oversized_payload_is_rejected_before_return(self):
        too_large = dtb(boot.KERNEL_ADDR - boot.DTB_ADDR + 1)
        with self.assertRaisesRegex(ValueError, "reserved RAM region"):
            self.generate(dtb=too_large)


if __name__ == "__main__":
    unittest.main()
