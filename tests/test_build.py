import hashlib
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
import uuid


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from m17s_armbian import build


class BuildUnitTests(unittest.TestCase):
    def test_source_parent_contract_points_at_project_root(self):
        source = Path(build.__file__).resolve().parents[2]
        self.assertEqual(source, PROJECT)
        self.assertTrue((source / "pyproject.toml").is_file())
        self.assertTrue((source / "sources.lock.json").is_file())
        self.assertTrue((source / "profiles/m17s.json").is_file())
        metadata = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["version"], build.__version__)

    def test_fstab_uses_only_supplied_filesystem_uuids(self):
        root_uuid = "12345678-1234-5678-9abc-def012345678"
        boot_uuid = "ABCD-1234"
        value = build.fstab(root_uuid, boot_uuid)
        self.assertEqual(
            value.splitlines(),
            [
                f"UUID={root_uuid} / ext4 defaults,noatime,errors=remount-ro 0 1",
                f"UUID={boot_uuid} /boot vfat defaults,umask=0077 0 2",
                "tmpfs /tmp tmpfs defaults,nosuid 0 0",
            ],
        )
        self.assertNotIn("/dev/", value)

    def test_uenv_binds_the_requested_root_and_fixed_payload_paths(self):
        root_uuid = "12345678-1234-5678-9abc-def012345678"
        value = build.uenv(root_uuid)
        self.assertIn(f"root=UUID={root_uuid}", value)
        self.assertIn("LINUX=/zImage", value)
        self.assertIn("INITRD=/uInitrd", value)
        self.assertIn(f"FDT=/{build.DTB}", value)
        self.assertNotIn("root=/dev/", value)

    def test_regular_and_allocate_refuse_unsafe_existing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ordinary = root / "ordinary.img"
            ordinary.write_bytes(b"image")
            self.assertEqual(build.regular(ordinary), ordinary)
            symlink = root / "link.img"
            symlink.symlink_to(ordinary)
            with self.assertRaises(ValueError):
                build.regular(symlink)
            with self.assertRaises(ValueError):
                build.regular(root)
            with self.assertRaisesRegex(ValueError, "overwrite"):
                build.allocate(ordinary, 10)
            with self.assertRaisesRegex(ValueError, "overwrite"):
                build.allocate(symlink, 10)

    def test_privacy_gate_accepts_minimum_clean_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "etc").mkdir()
            (root / "etc/machine-id").write_bytes(b"")
            (root / "etc/shadow").write_text("root:!:20000:0:99999:7:::\n")
            result = build.privacy_check(root)
            self.assertTrue(result["passed"])
            self.assertIn("root locked", result["checks"])

    def test_privacy_gate_rejects_identifiers_credentials_and_completion(self):
        cases = {
            "machine-id": ("etc/machine-id", b"machine-identity\n"),
            "ssh-host-key": ("etc/ssh/ssh_host_ed25519_key", b"private"),
            "authorized-key": ("home/user/.ssh/authorized_keys", b"ssh-ed25519 AAAA"),
            "network-profile": ("etc/NetworkManager/system-connections/home.nmconnection", b"wifi"),
            "provisioned": ("etc/m17s-armbian/provisioned", b"done\n"),
        }
        for label, (relative, content) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "etc").mkdir()
                (root / "etc/machine-id").write_bytes(b"")
                (root / "etc/shadow").write_text("root:!:20000:0:99999:7:::\n")
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "Privacy gate failed"):
                    build.privacy_check(root)

    def test_privacy_gate_rejects_any_systemd_autologin_dropin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "etc").mkdir()
            (root / "etc/machine-id").write_bytes(b"")
            (root / "etc/shadow").write_text("root:!:20000:0:99999:7:::\n")
            dropin = root / "etc/systemd/system/getty@.service.d/override.conf"
            dropin.parent.mkdir(parents=True)
            dropin.write_text(
                "[Service]\nExecStart=-/sbin/agetty --autologin root --noclear %I $TERM\n"
            )
            with self.assertRaisesRegex(ValueError, "automatic login"):
                build.privacy_check(root)

    def test_console_setup_dropin_orders_after_tmpfiles_without_replacing_vendor_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = root / "lib/systemd/system/console-setup.service"
            vendor.parent.mkdir(parents=True)
            vendor_contents = b"[Service]\nExecStart=/bin/true\n"
            vendor.write_bytes(vendor_contents)

            build.configure_console_setup_ordering(root)

            dropin = root / "etc/systemd/system/console-setup.service.d/m17s-tmpfiles.conf"
            self.assertTrue(dropin.is_file())
            self.assertFalse(dropin.is_symlink())
            self.assertEqual(
                dropin.read_text(encoding="utf-8"),
                "[Unit]\nAfter=systemd-tmpfiles-setup.service\n",
            )
            self.assertEqual(vendor.read_bytes(), vendor_contents)
            self.assertFalse((root / "etc/systemd/system/console-setup.service").exists())

    def test_source_lock_has_a_basic_immutable_input_manifest(self):
        lock = json.loads((PROJECT / "sources.lock.json").read_text())
        base = lock["base_image"]
        self.assertEqual(Path(base["url"]).name, base["name"])
        self.assertTrue(base["url"].startswith("https://github.com/ophub/"))
        self.assertRegex(base["sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(base["bytes"], 0)
        self.assertRegex(lock["builder_image"], r"@sha256:[0-9a-f]{64}$")
        uuid.UUID(build.USB_UUID)
        uuid.UUID(build.EMMC_UUID)
        self.assertNotEqual(build.USB_UUID, build.EMMC_UUID)

    def test_release_identity_uses_package_version_and_preserves_rc1_rule(self):
        self.assertEqual(
            build.USB_UUID,
            str(uuid.uuid5(uuid.NAMESPACE_URL,
                           f"https://m17s-armbian.local/{build.__version__}/usb")),
        )
        self.assertEqual(
            build.EMMC_UUID,
            str(uuid.uuid5(uuid.NAMESPACE_URL,
                           f"https://m17s-armbian.local/{build.__version__}/emmc-template")),
        )
        rc1_usb = str(uuid.uuid5(
            uuid.NAMESPACE_URL, "https://m17s-armbian.local/0.1.0rc1/usb"
        ))
        if build.__version__ == "0.1.0rc1":
            self.assertEqual(build.USB_UUID, rc1_usb)
        else:
            self.assertNotEqual(build.USB_UUID, rc1_usb)
        self.assertEqual(
            build.MOTD,
            f"M17S Armbian {build.__version__} — candidate image; kernel updates are gated.\n",
        )

    def test_build_source_has_no_host_paths_or_private_ipv4(self):
        text = Path(build.__file__).read_text(encoding="utf-8")
        self.assertNotIn('/Users/', text)
        self.assertNotRegex(text, r'\b10\.\d+\.\d+\.\d+\b')
        self.assertNotRegex(text, r'\b192\.168\.\d+\.\d+\b')

    def test_gzip_manifest_is_deterministic_and_self_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "payload.img"
            raw.write_bytes((b"m17s deterministic payload\n" * 64) + b"end")
            first = build.gzip_image(raw)
            compressed = root / first["file"]
            self.assertEqual(first["sha256"], build.digest(compressed))
            self.assertEqual(first["size"], compressed.stat().st_size)
            self.assertEqual(first["raw_sha256"], hashlib.sha256(raw.read_bytes()).hexdigest())
            self.assertEqual(first["raw_size"], raw.stat().st_size)


if __name__ == "__main__":
    unittest.main()
