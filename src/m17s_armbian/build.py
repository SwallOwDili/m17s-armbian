"""Build local image files in a pinned Linux container. Never flash a device."""
from __future__ import annotations

import argparse
import contextlib
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
import urllib.request
import uuid

from . import __version__
from .boot import generate_boot_files

ROOT_BYTES = 4 * 1024**3
BOOT_BYTES = 511 * 1024**2
USB_ROOT_SECTOR = 1056768
USB_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, f'https://m17s-armbian.local/{__version__}/usb'))
EMMC_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, f'https://m17s-armbian.local/{__version__}/emmc-template'))
KERNEL_RELEASE = '6.12.109-ophub'
DTB = 'dtb/amlogic/meson-gxl-s905x-p212.dtb'
MOTD = f'M17S Armbian {__version__} — candidate image; kernel updates are gated.\n'


def run(*args: str, **kwargs):
    return subprocess.run(list(args), check=True, **kwargs)


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_manifest(source: Path) -> dict[str, str]:
    files = sorted(list((source/'src/m17s_armbian').glob('*.py')) +
                   list((source/'assets').glob('*')) + list((source/'profiles').glob('*.json')) +
                   [source/'sources.lock.json'])
    return {str(path.relative_to(source)): digest(path) for path in files if path.is_file()}


def regular(path: Path) -> Path:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f'Only ordinary, non-symlink image files are accepted: {path}')
    return path


def fetch(entry: dict, cache: Path) -> Path:
    target = cache / entry['name']
    if not target.exists():
        temporary = target.with_suffix(target.suffix + '.partial')
        request = urllib.request.Request(entry['url'], headers={'User-Agent': 'm17s-armbian-builder'})
        with urllib.request.urlopen(request, timeout=120) as source, temporary.open('wb') as output:
            shutil.copyfileobj(source, output, 4 * 1024**2)
        regular(temporary)
        if temporary.stat().st_size != entry['bytes'] or digest(temporary) != entry['sha256']:
            raise ValueError('Downloaded base image does not match sources.lock.json')
        temporary.replace(target)
    regular(target)
    if target.stat().st_size != entry['bytes'] or digest(target) != entry['sha256']:
        raise ValueError('Cached base image does not match sources.lock.json')
    return target


@contextlib.contextmanager
def loop_mount(image: Path, mountpoint: Path, *, offset=0, size=0, readonly=False):
    regular(image)
    command = ['losetup', '--find', '--show', '--offset', str(offset)]
    if size:
        command += ['--sizelimit', str(size)]
    if readonly:
        command += ['--read-only']
    command += [str(image)]
    device = run(*command, capture_output=True, text=True).stdout.strip()
    mounted = False
    try:
        mountpoint.mkdir(parents=True, exist_ok=True)
        run('mount', '-o', 'ro' if readonly else 'rw', device, str(mountpoint))
        mounted = True
        yield device
    finally:
        try:
            if mounted:
                run('umount', str(mountpoint))
        finally:
            run('losetup', '-d', device)


def write(root: Path, name: str, content: str, mode=0o644):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    path.write_text(content)
    path.chmod(mode)


def remove(path: Path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def fstab(root_uuid: str, boot_uuid: str) -> str:
    return (f'UUID={root_uuid} / ext4 defaults,noatime,errors=remount-ro 0 1\n'
            f'UUID={boot_uuid} /boot vfat defaults,umask=0077 0 2\n'
            'tmpfs /tmp tmpfs defaults,nosuid 0 0\n')


def uenv(root_uuid: str) -> str:
    return (f'LINUX=/zImage\nINITRD=/uInitrd\nFDT=/{DTB}\n'
            f'APPEND=root=UUID={root_uuid} rootflags=data=writeback rw rootwait rootfstype=ext4 '
            'console=ttyAML0,115200n8 console=tty0 no_console_suspend consoleblank=0 '
            'fsck.fix=yes fsck.repair=yes net.ifnames=0 cgroup_enable=cpuset cgroup_memory=1 '
            'cgroup_enable=memory swapaccount=1 video=HDMI-A-1:1920x1080@60e plymouth.enable=0\n')


EXCLUDES = [
    '/boot/***', '/dev/***', '/proc/***', '/sys/***', '/run/***', '/tmp/***', '/mnt/***',
    '/root/.ssh/***', '/root/.*history', '/root/.not_logged_in_yet', '/root/.no_rootfs_resize',
    '/etc/ssh/ssh_host_*', '/etc/machine-id', '/var/lib/dbus/machine-id',
    '/etc/shadow', '/etc/shadow-', '/etc/gshadow-', '/etc/NetworkManager/system-connections/***',
    '/var/lib/NetworkManager/***', '/var/lib/dhcp/***', '/var/lib/systemd/random-seed',
    '/var/log/***', '/var/log.hdd/***', '/var/cache/apt/archives/*.deb',
    '/etc/armbian-image-release', '/root/.cache/***',
]


def customize(root: Path, base: Path, source: Path):
    # Refuse an upstream image that unexpectedly contains a personal account.
    accounts = (base / 'etc/passwd').read_text().splitlines()
    if any(1000 <= int(line.split(':')[2]) < 65534 for line in accounts):
        raise ValueError('Base image contains non-system user accounts')
    shadow = []
    for line in (base / 'etc/shadow').read_text().splitlines():
        parts = line.split(':')
        if parts[0] == 'root':
            parts[1] = '!'
            parts[2] = '20000'
        shadow.append(':'.join(parts))
    write(root, 'etc/shadow', '\n'.join(shadow) + '\n', 0o640)
    os.chown(root / 'etc/shadow', 0, 42)  # Debian shadow group
    write(root, 'etc/machine-id', '')
    (root / 'var/lib/dbus').mkdir(parents=True, exist_ok=True)
    (root / 'var/lib/dbus/machine-id').symlink_to('/etc/machine-id')
    for name in ['boot', 'dev', 'proc', 'sys', 'run', 'tmp', 'mnt', 'var/log', 'var/log.hdd',
                 'etc/NetworkManager/system-connections', 'var/lib/NetworkManager', 'var/lib/dhcp']:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / 'tmp').chmod(0o1777)
    write(root, 'etc/hostname', 'm17s\n')
    write(root, 'etc/hosts', '127.0.0.1 localhost\n127.0.1.1 m17s\n::1 localhost ip6-localhost ip6-loopback\n')
    write(root, 'etc/fstab', fstab(EMMC_UUID, '2026-EA01'))
    write(root, 'etc/m17s-armbian/image', __version__ + '\n')
    write(root, 'etc/m17s-armbian/kernel-release', KERNEL_RELEASE + '\n')
    shutil.copy2(source / 'profiles/m17s.json', root / 'etc/m17s-armbian/profile.json')
    shutil.copy2(source / 'sources.lock.json', root / 'etc/m17s-armbian/sources.lock.json')
    package = root / 'usr/local/lib/m17s-armbian/m17s_armbian'
    shutil.copytree(source / 'src/m17s_armbian', package, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for command, module in [('m17s-emmc', 'installer'), ('m17s-boot', 'maintenance'), ('m17s-firstboot', 'firstboot')]:
        write(root, f'usr/local/sbin/{command}', '#!/bin/sh\nexport PYTHONPATH=/usr/local/lib/m17s-armbian\n'
              f'exec python3 -m m17s_armbian.{module} "$@"\n', 0o755)
    for command in ['armbian-install', 'armbian-update', 'armbian-sync', 'armbian-kernel', 'armbian-tf']:
        original = root / 'usr/sbin' / command
        if original.exists():
            archive = root / 'usr/lib/m17s-armbian/upstream' / command
            archive.parent.mkdir(parents=True, exist_ok=True)
            original.rename(archive)
            archive.chmod(0o644)
        write(root, f'usr/sbin/{command}', '#!/bin/sh\necho "M17S: this upstream command is disabled. '
              'Use m17s-emmc or m17s-boot; kernel version upgrades are not supported in v0.1." >&2\nexit 2\n', 0o755)
    write(root, 'etc/apt/preferences.d/m17s-kernel',
          'Package: linux-image-* linux-headers-* linux-dtb-* linux-u-boot-* u-boot-*\nPin: version *\nPin-Priority: -1\n')
    for service in ['armbian-firstrun.service', 'armbian-resize-filesystem.service', 'rc-local.service']:
        path = root / 'etc/systemd/system' / service
        remove(path)
        path.symlink_to('/dev/null')
    for name in ['getty@.service.d', 'serial-getty@.service.d']:
        remove(root / 'etc/systemd/system' / name)
    # rc.local upstream restarts SSH and can resize the root partition behind the installer.
    remove(root / 'etc/profile.d/armbian-check-first-login.sh')
    write(root, 'etc/ssh/sshd_config.d/00-m17s.conf', 'PermitRootLogin no\n')
    conf = root / 'etc/ssh/sshd_config'
    conf.write_text('Include /etc/ssh/sshd_config.d/00-m17s.conf\n' + conf.read_text())
    write(root, 'etc/systemd/system/ssh.service.d/m17s-firstboot.conf',
          '[Unit]\nAfter=m17s-firstboot.service\nConditionPathExists=/etc/m17s-armbian/provisioned\n')
    write(root, 'etc/systemd/system/ssh.socket.d/m17s-firstboot.conf',
          '[Unit]\nAfter=m17s-firstboot.service\nConditionPathExists=/etc/m17s-armbian/provisioned\n')
    configure_console_setup_ordering(root)
    shutil.copy2(source / 'assets/m17s-firstboot.service', root / 'etc/systemd/system/m17s-firstboot.service')
    enabled = root / 'etc/systemd/system/multi-user.target.wants/m17s-firstboot.service'
    enabled.symlink_to('/etc/systemd/system/m17s-firstboot.service')
    write(root, 'etc/motd', MOTD)


def configure_console_setup_ordering(root: Path) -> None:
    """Keep console-setup temporary files until tmpfiles boot cleanup finishes."""
    write(root, 'etc/systemd/system/console-setup.service.d/m17s-tmpfiles.conf',
          '[Unit]\nAfter=systemd-tmpfiles-setup.service\n')


def privacy_check(root: Path) -> dict:
    bad = []
    for pattern in ['etc/ssh/ssh_host_*', 'root/.ssh/*', 'home/*/.ssh/*', '**/authorized_keys',
                    '**/id_rsa', '**/id_ed25519', 'root/.*history', 'home/*/.*history',
                    'etc/NetworkManager/system-connections/*', 'var/lib/dhcp/*',
                    'var/lib/NetworkManager/*', 'var/lib/systemd/random-seed']:
        bad.extend(str(path.relative_to(root)) for path in root.glob(pattern))
    if (root / 'etc/machine-id').read_bytes():
        bad.append('etc/machine-id is not empty')
    if (root / 'etc/m17s-armbian/provisioned').exists():
        bad.append('firstboot marker already exists')
    shadow = (root / 'etc/shadow').read_text()
    if not shadow.startswith('root:!:'):
        bad.append('root is not locked')
    if bad:
        raise ValueError('Privacy gate failed: ' + ', '.join(bad))
    for path in (root/'etc/systemd/system').rglob('*.conf'):
        if path.is_file() and '--autologin' in path.read_text(errors='replace'):
            raise ValueError(f'Unexpected automatic login configuration: {path.relative_to(root)}')
    return {'passed': True, 'checks': ['no SSH identities/authorized keys', 'empty machine-id',
            'no DHCP leases or NetworkManager connections', 'root locked', 'no firstboot completion'],
            'scope': 'file inventory and account state; not a universal secret detector'}


def allocate(path: Path, size: int):
    if path.exists() or path.is_symlink():
        raise ValueError(f'Refusing to overwrite {path}')
    with path.open('xb') as out:
        out.truncate(size)


def gzip_image(path: Path, output_dir: Path | None = None) -> dict:
    compressed = (output_dir or path.parent) / (path.name + '.gz')
    h = hashlib.sha256()
    with path.open('rb') as inp, compressed.open('xb') as out:
        with gzip.GzipFile(filename='', mode='wb', compresslevel=6, fileobj=out, mtime=0) as gz:
            while block := inp.read(4 * 1024**2):
                h.update(block)
                gz.write(block)
    return {'file': compressed.name, 'sha256': digest(compressed), 'size': compressed.stat().st_size,
            'raw_sha256': h.hexdigest(), 'raw_size': path.stat().st_size}


def copy_into(source: Path, dest: Path, offset: int):
    with source.open('rb') as inp, dest.open('r+b') as out:
        out.seek(offset)
        shutil.copyfileobj(inp, out, 4 * 1024**2)


def worker(source: Path, cache: Path, output: Path):
    snapshot = source_manifest(source)
    lock = json.loads((source / 'sources.lock.json').read_text())
    compressed = fetch(lock['base_image'], cache)
    baseimage = cache / 'verified-base.img'
    if baseimage.exists():
        baseimage.unlink()  # This builder-owned decompression is always recreated from verified input.
    with gzip.open(compressed, 'rb') as inp, baseimage.open('xb') as out:
        shutil.copyfileobj(inp, out, 4 * 1024**2)
    sector = baseimage.open('rb').read(512)
    partitions = [struct.unpack_from('<II', sector, 446 + n*16 + 8) for n in range(2)]
    if partitions != [(8192, 1046528), (1056768, 6144000)] or sector[510:512] != b'\x55\xaa':
        raise ValueError('Pinned base has an unexpected partition table')
    scratch = cache / 'build-work'
    scratch.mkdir()
    # Raw images use the explicitly selected cache filesystem, so a limited
    # Docker VM disk cannot fill while the host still has ample free space.
    image_dir = scratch
    rootimage = image_dir / f'm17s-emmc-rootfs-{__version__}.ext4.img'
    bootimage = image_dir / f'm17s-emmc-bootfs-{__version__}.vfat.img'
    usbimage = image_dir / f'm17s-usb-{__version__}.img'
    allocate(rootimage, ROOT_BYTES)
    run('mkfs.ext4', '-q', '-F', '-L', 'M17S_ROOT', '-U', EMMC_UUID,
        '-E', 'lazy_itable_init=0,lazy_journal_init=0', str(rootimage))
    allocate(bootimage, BOOT_BYTES)
    run('mkfs.vfat', '-F', '32', '-n', 'BOOT_EMMC', '-i', '2026ea01', str(bootimage))
    baseboot, baseroot, root, boot = [scratch / name for name in ['baseboot', 'baseroot', 'root', 'boot']]
    with loop_mount(baseimage, baseboot, offset=4194304, size=BOOT_BYTES, readonly=True), \
         loop_mount(baseimage, baseroot, offset=541065216, size=3145728000, readonly=True), \
         loop_mount(rootimage, root), loop_mount(bootimage, boot):
        run('rsync', '-aHAX', '--numeric-ids', *[f'--exclude={p}' for p in EXCLUDES],
            str(baseroot) + '/', str(root) + '/')
        customize(root, baseroot, source)
        privacy = privacy_check(root)
        for name in ['zImage', 'uInitrd', 'u-boot-p212.bin', DTB, f'config-{KERNEL_RELEASE}',
                     f'System.map-{KERNEL_RELEASE}', f'initrd.img-{KERNEL_RELEASE}']:
            dest = boot / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(baseboot / name, dest)
        files = generate_boot_files((boot/'u-boot-p212.bin').read_bytes(), EMMC_UUID,
                                    (boot/'zImage').read_bytes(), (boot/'uInitrd').read_bytes(), (boot/DTB).read_bytes())
        # The installation payload must not be active until its target UUID has been bound.
        write(boot, 'uEnv.txt', uenv(EMMC_UUID))
        write(boot, 'M17S-UNACTIVATED.txt', 'Partition payload: use m17s-emmc. No boot activation script is included.\n')
        write(output, 'boot-validation.json', files['manifest.json'].decode())
        (scratch/'usb-upstream').mkdir()
        for name in ['boot.cmd', 'boot.scr', 's905_autoscript', 's905_autoscript.cmd',
                     'aml_autoscript', 'aml_autoscript.cmd']:
            shutil.copy2(baseboot/name, scratch/'usb-upstream'/name)
        run('sync')
    # All source mounts have closed. Release this large disposable cache before
    # assembling the USB image; the verified compressed upstream input remains.
    baseimage.unlink()
    run('e2fsck', '-fn', str(rootimage))
    run('fsck.vfat', '-n', str(bootimage))
    allocate(usbimage, USB_ROOT_SECTOR*512 + ROOT_BYTES)
    mbr = bytearray(512)
    for n, (kind, start, count) in enumerate([(0x0c,8192,1046528),(0x83,USB_ROOT_SECTOR,ROOT_BYTES//512)]):
        struct.pack_into('<B3sB3sII', mbr, 446+16*n, 0, b'\xfe\xff\xff', kind, b'\xfe\xff\xff', start, count)
    mbr[510:512] = b'\x55\xaa'
    with usbimage.open('r+b') as stream:
        stream.write(mbr)
    copy_into(bootimage, usbimage, 4194304)
    copy_into(rootimage, usbimage, USB_ROOT_SECTOR*512)
    with loop_mount(usbimage, root, offset=USB_ROOT_SECTOR*512, size=ROOT_BYTES) as rootdev, \
         loop_mount(usbimage, boot, offset=4194304, size=BOOT_BYTES):
        write(root, 'etc/fstab', fstab(USB_UUID, '2026-AB01'))
        write(root, 'etc/m17s-armbian/media', 'usb\n')
        for name in ['boot.cmd', 'boot.scr', 's905_autoscript', 's905_autoscript.cmd']:
            shutil.copy2(scratch/'usb-upstream'/name, boot/name)
        # Multiboot activation writes vendor environment; keep opt-in files out of the boot root.
        (boot/'multiboot-activation').mkdir()
        for name in ['aml_autoscript', 'aml_autoscript.cmd']:
            shutil.copy2(scratch/'usb-upstream'/name, boot/'multiboot-activation'/name)
        shutil.copy2(boot/'u-boot-p212.bin', boot/'u-boot.ext')
        write(boot, 'uEnv.txt', uenv(USB_UUID))
        remove(boot/'M17S-UNACTIVATED.txt')
        write(boot, 'FIRST-BOOT.txt', 'M17S candidate image. No default password.\n'
              'Use HDMI + USB keyboard, or add firstboot.json with hostname, username, ssh_authorized_keys.\n'
              'eMMC is never installed automatically. See project README.\n')
        privacy_check(root)
        run('sync')
    # UUID mutation must happen with ext4 unmounted.
    loop = run('losetup', '--find', '--show', '--offset', str(USB_ROOT_SECTOR*512),
               '--sizelimit', str(ROOT_BYTES), str(usbimage), capture_output=True, text=True).stdout.strip()
    try:
        run('tune2fs', '-U', USB_UUID, loop)
        run('e2fsck', '-fn', loop)
    finally:
        run('losetup', '-d', loop)
    loop = run('losetup', '--find', '--show', '--offset', '4194304',
               '--sizelimit', str(BOOT_BYTES), str(usbimage), capture_output=True, text=True).stdout.strip()
    try:
        run('fatlabel', '-i', loop, '2026ab01')
        run('fatlabel', loop, 'BOOT_USB')
        run('fsck.vfat', '-n', loop)
    finally:
        run('losetup', '-d', loop)
    with loop_mount(usbimage, boot, offset=4194304, size=BOOT_BYTES, readonly=True):
        if (boot/'emmc_autoscript').exists():
            raise ValueError('USB must not contain an eMMC activation script')
    artifacts = {}
    for name, path in [('emmc_root', rootimage), ('emmc_boot', bootimage), ('usb', usbimage)]:
        print(f'Compressing {path.name}', flush=True)
        artifacts[name] = gzip_image(path, output)
    report = {'schema_version':1, 'version':__version__, 'board':'m17s-s905x-2g-emmc8g',
              'status':'candidate-not-hardware-tested', 'kernel_release':KERNEL_RELEASE,
              'artifacts':artifacts, 'sources':lock, 'source_files':snapshot, 'privacy':privacy,
              'reproducibility':'pinned inputs and recipe; whole filesystem byte identity not established',
              'signature':'unsigned local candidate; verify hashes through a trusted distribution channel'}
    if source_manifest(source) != snapshot:
        raise ValueError('Source changed during build; refuse to label these artifacts a completed release')
    write(output, 'release.json', json.dumps(report, indent=2, sort_keys=True)+'\n')
    checksums = ''.join(f"{entry['sha256']}  {entry['file']}\n" for entry in artifacts.values())
    checksums += f"{digest(output/'release.json')}  release.json\n"
    checksums += f"{digest(output/'boot-validation.json')}  boot-validation.json\n"
    write(output, 'SHA256SUMS', checksums)
    for path in [rootimage, bootimage, usbimage]:
        path.unlink()
    shutil.rmtree(scratch)
    print('Build and filesystem checks complete. Images have not been booted on hardware.', flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=Path('cache'))
    parser.add_argument('--output', type=Path, default=Path('dist'))
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[2]
    if not (source/'sources.lock.json').is_file():
        parser.error('Run the builder from the source checkout with PYTHONPATH=src')
    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        parser.error('--output must be an empty directory (refuses overwrites)')
    if args.worker:
        if sys.platform != 'linux' or os.geteuid() != 0 or not Path('/.dockerenv').exists():
            parser.error('The image worker runs only as root inside Docker')
        worker(source, args.cache.resolve(), args.output.resolve())
        return 0
    lock = json.loads((source / 'sources.lock.json').read_text())
    fetch(lock['base_image'], args.cache.resolve())
    run('docker', 'run', '--rm', '--privileged', '--platform', lock['builder_platform'],
        '--entrypoint', 'python3', '-e', 'PYTHONPATH=/src/src',
        '-e', f"SOURCE_DATE_EPOCH={lock['source_date_epoch']}",
        '-v', f'{source}:/src:ro', '-v', f'{args.cache.resolve()}:/cache',
        '-v', f'{args.output.resolve()}:/out', lock['builder_image'],
        '-m', 'm17s_armbian.build', '--worker', '--cache', '/cache', '--output', '/out')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
