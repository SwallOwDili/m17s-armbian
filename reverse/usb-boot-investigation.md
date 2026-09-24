# 天猫魔盒 M17S 刷入 Armbian：USB 启动调查记录

> 这是 USB 启动打通阶段的历史记录。当前项目已使用 `m17s-emmc` 完成真实 eMMC 安装，最终接线、安装门禁和回滚边界以根 README 与 `docs/INSTALLER.md` 为准。

设备：MagicBox_M17 / M17S，Amlogic S905X，2GB RAM，8GB eMMC
原厂系统：YunOS 6.1.0（Android 5.1.1 / SDK 22，内核 3.14.29）
最终结果：**从 USB 启动 Armbian 26.11.0（Debian bookworm，内核 6.12.109-ophub）**

---

## 一、结果

```text
root@armbian:~# uname -m ; uname -r
aarch64
6.12.109-ophub

root@armbian:~# cat /proc/device-tree/model
Amlogic Meson GXL (S905X) P212 Development Board

Mem: 1.9Gi（可用 1.5Gi）    rootfs:/dev/sda2 14G  剩余 12G
```

访问：`ssh root@<盒子的IP>`，密码在首次登录时设置。

---

## 二、关键结论：为什么原厂 Android 上无法用软件提权

逐项实测，全部封死：

| 路径 | 结果 |
|---|---|
| `adb root` | 拒绝（`ro.secure=1`、`ro.debuggable=0`、`build.type=user`） |
| `system_control` 写接口 | 拒绝（shell 无 `droidlogic.permission.SYSTEM_CONTROL`） |
| `pm enable` 系统组件 | 拒绝（跨包组件状态不可改） |
| `settings put secure` | 拒绝（无 `WRITE_SECURE_SETTINGS`） |
| `svc power reboot` | 拒绝（无 `REBOOT`） |
| 自签 OTA | 关闭（`otacerts.zip` 使用 Alibaba YunOSTV 发布证书，非 AOSP 公开测试证书） |
| Dirty COW | 未复现（30 万次写 + 30 万次 madvise，0 错误，后备文件未变） |
| `service.adb.root=1` + 重启 adbd | adbd 确实重启（PID 变化），但仍降权为 shell，构建未开 `ALLOW_ADBD_ROOT` |
| root 网络服务 `9751` / `3988` / `127.0.0.1:3999` | 静默或仅推送，无可用命令接口 |
| 工厂测试应用（uid 1000，有 `SYSTEM_CONTROL`） | 无命令入口，且不碰引导环境 |
| 其它系统应用 | 无一操作 `ubootenv` / `bootcmd` |

---

## 三、真正的突破口：调试串口上挂着 root shell

原厂固件有一条**以 root 运行的 console 服务**：

```text
进程       root  3413  1  /system/bin/sh
init 服务  [init.svc.console]: [running]
控制台设备 /sys/class/tty/console/active = ttyS0
内核参数   console=ttyS0,115200  earlyprintk=aml-uart,0xc81004c0
```

即：**该串口既是内核控制台，也是一个 root shell 的输入输出**。

### 串口接线（实测确定，非推断）

```text
调试器 GND  →  HDMI 金属外壳
调试器 RXD  →  主板背面 4 焊盘中「左下」那个
调试器 TXD  →  主板背面 4 焊盘中「右上」那个
调试器 VCC  →  必须悬空
```

参数：**115200 8N1，3.3V TTL**

排错要点：
- **GND 接错会导致完全收不到数据**。曾用 HDMI 外壳以外的推断点，长时间无任何输出。
- 探针要用胶带压紧，接触不良会表现为「一会儿有数据一会儿没有」。
- 接线正确时，开机即可看到完整启动日志，末尾会出现 `root@MagicBox_M17:/ #`。

---

## 四、完整步骤

### 1. 准备 Armbian U 盘

用 `ophub/amlogic-s9xxx-armbian` 的 **S905X** 镜像写入 U 盘（≥8GB）：

```bash
# macOS 写入
gzip -dc Armbian_*_amlogic_s905x_bookworm_*.img.gz > armbian.img
sudo diskutil unmountDisk /dev/diskN
sudo dd if=armbian.img of=/dev/rdiskN bs=4m conv=sync
```

确认启动分区里的 `uEnv.txt` 含：

```text
FDT=/dtb/amlogic/meson-gxl-s905x-p212.dtb
```

这一项必须对应 `gxl_p212_2g`。

### 2. 生成 u-boot.ext（**最容易漏的一步**）

U 盘启动分区里有 `u-boot-p212.bin`，但**没有 `u-boot.ext`**。原厂 U-Boot 会先找 `u-boot.ext`，找不到就直接用自己的 `booti` 引导 Linux——而它要往设备树里写 Amlogic 私有节点（`rsvmem`/`bl31`），标准设备树里没有这些节点，于是：

```text
libfdt fdt_path_offset() returned FDT_ERR_NOTFOUND
[rsvmem] bl31 reserved memory set addr error.
"Synchronous Abort" handler, esr 0x96000210     ← U-Boot 崩溃，无限重启
```

**解决办法**：把 `u-boot-p212.bin` 复制为 `u-boot.ext`。

```bash
cp /Volumes/BOOT/u-boot-p212.bin /Volumes/BOOT/u-boot.ext
```

有了它，原厂 U-Boot 会加载主线 U-Boot 并跳转过去，绕开上述设备树修补，由主线 U-Boot 完成引导。

### 3. 修改 U-Boot 环境变量

先备份（重要）：

```sh
dd if=/dev/block/env of=/data/local/tmp/env.bin
```

然后通过串口 root shell 写入（`system_control` 在 root 身份下权限校验通过）：

```sh
dumpsys system_control -b set ubootenv.var.start_usb_autoscript 'for usbdev in 0 1 2 3; do if fatload usb ${usbdev} 1020000 s905_autoscript; then autoscr 1020000; fi; done'
dumpsys system_control -b set ubootenv.var.start_mmc_autoscript 'if fatload mmc 0 1020000 s905_autoscript; then autoscr 1020000; fi;'
dumpsys system_control -b set ubootenv.var.start_emmc_autoscript 'if fatload mmc 1 1020000 emmc_autoscript; then autoscr 1020000; fi;'
dumpsys system_control -b set ubootenv.var.start_autoscript 'if mmcinfo; then run start_mmc_autoscript; fi; if usb start; then run start_usb_autoscript; fi; run start_emmc_autoscript'
dumpsys system_control -b set ubootenv.var.bootcmd 'run start_autoscript; run storeboot'
dumpsys system_control -b set ubootenv.var.upgrade_step 2
```

**回退设计（USB 启动阶段）**：`bootcmd` 的第二段仍是 `run storeboot`。当时 eMMC 仍是原厂 Android，拔掉 U 盘即可回退；完成当前项目的 eMMC 安装后不再具备这条 Android 回退路径。

### 4. 启动

插上 U 盘，断电重启。串口依次出现：

```text
reading s905_autoscript
start amlogic old u-boot
reading u-boot.ext          ← 主线 U-Boot 被加载
（主线 U-Boot 接管）
reading uEnv.txt
reading /zImage
Starting kernel ...
```

约 2 分钟后系统起来，自动 DHCP 拿到 IP，SSH 可用。

首次登录会要求设置 root 密码，并选择默认 shell（选 1 = bash），随后可选择跳过用户创建（Ctrl+C）。

---

## 五、恢复原厂系统

```sh
# 从 /data/local/tmp/env.bin 恢复整个 env 分区（需先 root）
dd if=/data/local/tmp/env.bin of=/dev/block/env
```

或只改回 `bootcmd`：

```sh
dumpsys system_control -b set ubootenv.var.bootcmd 'set_usb_boot 4;run storeboot'
```

该结论只适用于当时的 USB 启动阶段；当前样机的原厂 Android 已被 Linux 分区替代，恢复应使用安装前完整备份。

---

## 六、写入 eMMC

当前项目使用带设备探测、完整备份、计划绑定和读回校验的 `m17s-emmc`，不沿用早期记录里的通用安装入口：

```sh
sudo m17s-emmc probe --target /dev/mmcblkX
sudo m17s-emmc plan --target /dev/mmcblkX --release-dir /path/to/release --output /path/to/plan.json
```

完整流程见 `docs/INSTALLER.md`。

---

## 七、踩过的坑（按耗时排序）

1. **误以为 HDMI 外壳是有效接地** —— 推断而非实测，导致长时间完全收不到数据。
2. **不知道 `u-boot.ext` 是必需项** —— 表现为 U-Boot 崩溃重启循环，日志里 `FDT_ERR_NOTFOUND` 是关键线索。
3. **内核 `quiet` 参数** —— 串口在启动后基本静默，所以调试必须在**上电瞬间**进行。
4. **原厂 U-Boot 不接受键盘输入** —— 自动启动无法用回车或 Ctrl+C 打断，因此只能靠 `bootcmd` 的回退设计来脱困。
5. **探针接触不良** —— 表现为间歇性有无数据，必须固定牢靠。
