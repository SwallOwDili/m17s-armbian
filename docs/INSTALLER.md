# M17S eMMC installer v0.1 scope

The exact USB boot modifications, partition payload construction, and two-stage eMMC RAM boot chain are documented in [BOOT_CHAIN.md](BOOT_CHAIN.md).

This document follows the `0.1.0rc3` source. The new backup-GPT gate and complete passive-file readback have unit coverage but await rebuilt-image and hardware validation.

`m17s-emmc` installs only the `m17s-s905x-2g-emmc8g` profile. Production
probing requires a non-removable `8GME4R` MMC device of exactly 7,818,182,656
bytes with 512-byte logical sectors, boot0/boot1 devices, the P212 compatible
string, and 2 GiB described RAM. The target cannot be the running USB/root disk
and none of its partitions may be mounted, swap, or held by another block
device.

The default mode accepts only an existing MBR layout with partition 1 starting
at sector 1,433,600 and spanning 1,046,528 sectors, and partition 2 starting at
sector 2,482,176 and extending to the final sector. It never changes that
partition table or the first 700 MiB.

The existing-layout path validates both views of the layout: sysfs partition
geometry and the on-disk MBR must agree. The MBR must have the `55aa` signature,
FAT32-LBA type `0x0c` for p1, Linux type `0x83` for p2, matching start/length
fields, and empty p3/p4 entries. A GPT primary header at LBA1 or backup header in the final logical sector is refused.

`plan --initialize-layout` is a separate, explicit path for a factory device
with no kernel-visible partitions, zero bytes at MBR offsets 446 through 511,
and neither GPT header. It writes exactly those 66 MBR bytes. Bytes `[0,446)` and
`[512,700 MiB)`, boot0, and boot1 must retain their pre-install SHA-256 hashes.
Any other existing layout is refused. This release does not migrate a factory
Android layout or repair a missing vendor signed boot chain.

Before `apply`, `backup` writes and re-reads gzip archives of the complete MMC
user area, boot0, and boot1 to storage outside the target. `apply` requires that
manifest, re-verifies every archive, re-probes all plan-bound device identity,
and requires the exact interactive phrase `ERASE <device> <full CID>`. There is
no `-y` option.

Both payload gzip files are fully expanded and verified in ordinary temporary
files under the mandatory external `--work-dir` before the first target write.
The work directory must have room for both 511 MiB and 4 GiB raw images and may
not reside on the target. The root image is written and read back,
checked, expanded, assigned a random UUID, and receives a new fstab. The boot
image is then written and read back. It must not contain `emmc_autoscript`.
Boot files are generated from `u-boot-p212.bin`, `zImage`, `uInitrd`, and the
P212 DTB. Every generated boot file except `emmc_autoscript`, plus the complete
rewritten `uEnv.txt` and optional `firstboot.json`, is compared byte-for-byte after a
read-only remount. `emmc_autoscript` is installed last, then the complete managed
set is compared again after another read-only remount.

For a headless first boot, pass `--firstboot-config PATH`. The file is parsed by
the same strict first-boot validator and must contain at least one SSH public
key; its normalized JSON is copied to the target boot filesystem and verified
byte-for-byte before and after activation. Without this option, no account, key, or password is copied from
the USB system and the first eMMC boot requires HDMI/keyboard provisioning.

`release.json` hashes detect damaged or mismatched local artifacts. This release
does not verify a cryptographic publisher signature; its `signature` metadata
is descriptive text, not an authentication mechanism.

## External space and an optional Mac share

The USB root filesystem is fixed at 4 GiB. It cannot hold the 4.56 GiB of
expanded boot and root payloads required by `apply`, and it is far too small
for the complete eMMC backup. Use storage outside both the USB root and target
eMMC. The backup preflight requires a worst-case 7.35 GiB free; the work
directory needs another 4.56 GiB after the backup exists. At least 16 GiB free
is a practical minimum when the release files, backup, and work area share one
filesystem. The backup output directory must not exist yet, while `--work-dir`
must already exist. Neither may be on the target eMMC.

On a Mac, `tools/reverse_sshfs.py` is an optional way to supply that external
space over the existing Mac-to-box SSH connection. It connects the Mac
`/usr/libexec/sftp-server` to sshfs passive mode through anonymous pipes and
opens no listener. The box needs FUSE and sshfs, an existing mountpoint, and
non-interactive sudo for the remote sshfs command. Debian sshfs 3.7.3 is known
to support this path; this compatibility observation is not rc2 image or
installation acceptance.

Prepare the box once:

```sh
sudo apt-get install sshfs fuse3
sudo mkdir -p /mnt/mac-m17s
sudo -n true
```

Then keep this foreground process running on the Mac. `caffeinate` keeps the
Mac awake for the long backup and readback. Read/write access is deliberately
opt-in because the installer must create its backup and work files:

```sh
caffeinate -dims python3 tools/reverse_sshfs.py \
  --hostuser operator@m17s-box.example \
  --known-hosts /path/to/task-known_hosts \
  --local-dir /path/to/m17s-external \
  --remote-mount /mnt/mac-m17s \
  --read-write
```

Use a dedicated local directory and keep private keys outside it. The helper
also accepts `--identity` and an absolute `--remote-sshfs` path. Without
`--read-write`, both the local SFTP server and remote sshfs remain read-only.
Run the existing per-device `probe`, `plan`, `backup`, and interactive `apply`
commands with the release, plan, backup, and work paths below
`/mnt/mac-m17s`; a public-key `firstboot.json` may still be passed with
`--firstboot-config /boot/firstboot.json`.

After the installer has finished and no process is using the share, unmount on
the box and then stop the Mac foreground process with Ctrl-C:

```sh
sudo umount /mnt/mac-m17s
```
