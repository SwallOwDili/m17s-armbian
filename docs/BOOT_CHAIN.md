# M17S USB 启动与 eMMC 安装改动

本文只回答两件事：原厂 M17S 为什么能从项目 U 盘启动，以及运行在 U 盘上的系统如何把项目镜像安装到 eMMC。

USB/eMMC 启动链已在 rc2 样机上验证；主/备 GPT 双检查和完整被动文件读回属于当前 rc3 源码，已通过单元测试，尚待新镜像与实机回归。

## 1. 原始状态

样机是 Amlogic S905X、2 GiB RAM、Samsung 8 GB eMMC，原厂 U-Boot 负责签名启动链。项目没有覆盖 eMMC 前部的厂商引导器，也没有把通用整盘镜像直接写入 eMMC。

原厂 U-Boot 已具备从 FAT 分区加载 Amlogic script 的能力，但默认 `bootcmd` 不会稳定进入项目 U 盘启动链。直接让原厂 U-Boot 用自己的 `booti` 启动标准 P212 DTB，还会出现：

```text
libfdt fdt_path_offset() returned FDT_ERR_NOTFOUND
[rsvmem] bl31 reserved memory set addr error
Synchronous Abort
```

原因是原厂 U-Boot 尝试向标准 DTB 写入私有 `rsvmem`/`bl31` 节点。

## 2. 为 USB 启动改了什么

### 2.1 一次性修改原厂 U-Boot 环境

通过原厂系统的串口 root console 写入以下变量：

```text
start_usb_autoscript=for usbdev in 0 1 2 3; do if fatload usb ${usbdev} 1020000 s905_autoscript; then autoscr 1020000; fi; done
start_mmc_autoscript=if fatload mmc 0 1020000 s905_autoscript; then autoscr 1020000; fi;
start_emmc_autoscript=if fatload mmc 1 1020000 emmc_autoscript; then autoscr 1020000; fi;
start_autoscript=if mmcinfo; then run start_mmc_autoscript; fi; if usb start; then run start_usb_autoscript; fi; run start_emmc_autoscript
bootcmd=run start_autoscript; run storeboot
upgrade_step=2
```

关键点：

- `bootcmd` 先尝试外部启动脚本，失败后仍执行 `storeboot`。
- USB 插着时加载 `s905_autoscript`；当时拔掉 USB 可回退到原厂系统。
- `start_emmc_autoscript` 为后续 eMMC FAT 分区上的 `emmc_autoscript` 留出入口。
- 这是设备级一次性 multiboot 激活，不是每次构建镜像时重复写环境。

仓库保留上游 `aml_autoscript` 到 USB 的 `multiboot-activation/` 子目录，但不会自动执行它。

### 2.2 修改 USB 启动分区

构建器不是就地修改一份正在使用的 U 盘，而是验证锁定的上游镜像后重新组装 MBR、FAT bootfs 和 ext4 rootfs。USB 整盘布局为：

| 分区 | MBR 类型 | 起始扇区 | 长度 |
| --- | --- | ---: | ---: |
| p1 FAT | `0x0c` | `8,192`（4 MiB） | `1,046,528` 扇区（511 MiB） |
| p2 ext4 | `0x83` | `1,056,768` | `8,388,608` 扇区（4 GiB） |

构建器在 FAT 启动分区完成这些修改：

1. 保留上游 `boot.scr`、`s905_autoscript` 及其源码。
2. 将 `u-boot-p212.bin` 复制成 `u-boot.ext`。
3. 写入项目 USB 根分区 UUID、串口参数和固定 1080p HDMI 参数的 `uEnv.txt`：

```text
LINUX=/zImage
INITRD=/uInitrd
FDT=/dtb/amlogic/meson-gxl-s905x-p212.dtb
APPEND=root=UUID=<USB_ROOT_UUID> rootflags=data=writeback rw rootwait rootfstype=ext4 console=ttyAML0,115200n8 console=tty0 no_console_suspend consoleblank=0 fsck.fix=yes fsck.repair=yes net.ifnames=0 cgroup_enable=cpuset cgroup_memory=1 cgroup_enable=memory swapaccount=1 video=HDMI-A-1:1920x1080@60e plymouth.enable=0
```

4. 明确检查 USB FAT 中不存在 `emmc_autoscript`，防止插入 U 盘就自动安装 eMMC。

`u-boot.ext` 是决定性改动。原厂 U-Boot 先读取它，把控制权交给主线 P212 U-Boot，再由主线 U-Boot 读取 `uEnv.txt`、`zImage`、`uInitrd` 和 P212 DTB，从而绕过原厂 `booti` 修改 DTB 时的崩溃。

最终 USB 启动链：

```text
原厂签名 U-Boot
  -> bootcmd / start_autoscript
  -> USB:s905_autoscript
  -> USB:u-boot.ext（u-boot-p212.bin）
  -> USB:uEnv.txt
  -> zImage + uInitrd + meson-gxl-s905x-p212.dtb
  -> USB ext4 根分区
```

### 2.3 修改 USB 根文件系统

项目不是把上游 Armbian 原样发布。构建器还会：

- 复制 `m17s_armbian` Python 包到 `/usr/local/lib/m17s-armbian/`。
- 安装 `m17s-emmc`、`m17s-boot`、`m17s-firstboot` 三个入口。
- 安装 M17S profile、来源锁文件和 firstboot systemd 服务。
- 清除 SSH 主机密钥、machine-id、DHCP/NetworkManager 状态、日志和历史文件。
- 锁定 root，不设置默认密码；首次启动通过 HDMI 键盘或公钥 JSON 创建普通用户。
- 禁用通用 `armbian-install`、`armbian-update`、`armbian-sync`、`armbian-kernel`、`armbian-tf`，避免绕过 M17S 的引导区保护。
- 将 USB 根 UUID、`fstab` 和介质标记改成 USB 专用值。

相关实现位于 `src/m17s_armbian/build.py`。

## 3. 构建器生成哪些安装产物

同一次构建产生三份镜像：

```text
m17s-usb-<version>.img.gz
m17s-emmc-bootfs-<version>.vfat.img.gz
m17s-emmc-rootfs-<version>.ext4.img.gz
```

- 第一份是可写入 U 盘的整盘镜像。
- 后两份是 eMMC 分区载荷，不是 eMMC 整盘镜像。
- eMMC bootfs 固定 511 MiB；rootfs 模板固定 4 GiB，安装后扩到分区末尾。
- bootfs 模板故意不含激活文件 `emmc_autoscript`，半成品不会变成可启动安装。

## 4. U 盘系统如何安装 eMMC

### 4.1 保留哪些原厂区域

安装器只写两个 Linux 分区：

```text
0                         700 MiB               1211 MiB        1212 MiB
| 厂商签名引导/环境/保留区 | p1 FAT，511 MiB      | 分区间隙      | p2 ext4 到盘尾
```

精确布局：

- p1 起始扇区 `1,433,600`，长度 `1,046,528` 扇区。
- p2 起始扇区 `2,482,176`，延伸到 eMMC 最后一个扇区。
- 默认安装不改分区表和前 700 MiB。
- `boot0`、`boot1` 和 p1/p2 之间的间隙也不写入。

空白分区表场景必须显式使用 `--initialize-layout`；安装器同时检查 LBA1 的 GPT 主头和末扇区的 GPT 备份头，两者都不存在才进入该路径。初始化只写 MBR 的 446–511 字节，并在写前后验证其余受保护区域哈希。

### 4.2 安装前门禁

`m17s-emmc` 只接受实测 profile：

- 非可移动 MMC，产品名 `8GME4R`。
- 总容量 `7,818,182,656` 字节，512 字节逻辑扇区。
- 设备树含 `amlogic,p212`，内存为 2 GiB。
- 目标不是当前 USB 根盘，没有挂载、swap 或 holder。
- 分区布局、MBR 类型和 LBA 必须与上面的实测值一致。

安装顺序必须是 `probe -> plan -> backup -> apply`。备份覆盖完整 user area、boot0、boot1，写后重新读取并用 SHA-256 校验；`apply` 还要求输入 `ERASE <device> <full CID>`。

### 4.3 实际写入顺序

1. 在外部 `--work-dir` 完整解压 bootfs/rootfs，并核对压缩与原始 SHA-256；此时尚未写目标盘。
2. 再次探测并确认目标设备与计划一致。
3. 记录前 700 MiB、boot0、boot1 和分区间隙的保护哈希。
4. 写 p2 rootfs，读回校验，扩展 ext4，生成随机根 UUID。
5. 更新目标 rootfs 的 `fstab` 和介质标记，再只读挂载复查。
6. 写 p1 bootfs，读回校验。
7. 根据最终根 UUID 生成两阶段 eMMC 启动文件；先写本次生成的全部被动文件，并逐字节复查生成文件、完整 `uEnv.txt` 和可选 `firstboot.json`。
8. 再次验证所有受保护区域未变化。
9. 最后才写 `emmc_autoscript` 激活启动，并再次只读复查完整受管文件集合。

### 4.4 eMMC 两阶段启动链

原厂 U-Boot 仍然是第一阶段信任根。它从 eMMC p1 读取 `emmc_autoscript`，把以下文件装入固定 RAM 地址：

| 文件 | 地址 |
| --- | --- |
| `u-boot-m17s-ram.bin` | `0x01000000` |
| P212 DTB | `0x08008000` |
| `zImage` | `0x08080000` |
| `uInitrd` | `0x13000000` |
| `m17s-ramboot.scr` | `0x21000000` |

`u-boot-m17s-ram.bin` 来自经过固定大小和 SHA-256 验证的 `u-boot-p212.bin`。项目只在这个**加载到 RAM 的副本**中做一个等长修改：

```text
bootcmd=run distro_bootcmd
                  ↓
bootcmd=source 0x021000000    # 数值等于 0x21000000；前导 0 用于保持字符串等长
```

它没有覆盖 eMMC 前 700 MiB 中的厂商 U-Boot。原厂 U-Boot 加载全部载荷后 `go 0x01000000`；主线 U-Boot 启动后执行已预载的 `m17s-ramboot.scr`，设置最终根 UUID 和内核参数，再执行：

```text
booti 0x08080000 0x13000000 0x08008000
```

最终 eMMC 启动链：

```text
原厂签名 U-Boot（保留）
  -> eMMC:emmc_autoscript
  -> 把内核/initrd/DTB/第二阶段脚本/主线 U-Boot 装入 RAM
  -> RAM 中的 u-boot-m17s-ram.bin
  -> source 0x21000000
  -> booti
  -> eMMC p2 根分区
```

实现对应：

- USB 镜像与分区载荷：[`build.py`](../src/m17s_armbian/build.py)
- 两阶段启动文件：[`boot.py`](../src/m17s_armbian/boot.py)
- 安装门禁、备份和写入事务：[`installer.py`](../src/m17s_armbian/installer.py)
- 启动文件校验与更新：[`maintenance.py`](../src/m17s_armbian/maintenance.py)
- 早期实机调查：[`usb-boot-investigation.md`](../reverse/usb-boot-investigation.md)
