# 0.1.0rc2 验证范围

> 本页是 rc2 镜像的历史验证记录。当前 `0.1.0rc3` 源码包含安装器修正，尚未重建镜像或完成同等级 loop/实机回归。

本地候选版已在一台 M17S / S905X / 2 GiB / 8GME4R 样机上完成下述安装与启动测试，
保留 USB 启动卡住和 Bluetooth 初始化失败两个已知问题，尚未公开发布。
发行目录的 `validation/hardware.json` 和 `validation/HARDWARE.md`
是精选、脱敏的实机证据；它们绑定不可变 `release.json` 的 SHA-256。
`release.json` 保留构建时的 `candidate-not-hardware-tested`，`local-tests.json` 也保留当时的
`hardware_status: in-progress`；后续硬件状态以补充报告为准。

## 构建与自动检查

- 61 项单元测试通过，覆盖启动载荷边界、脚本生成、首启配置、安装门禁及启动维护。
- 固定 ARM64 容器构建通过，ext4/VFAT 检查通过；三份最终 rc2 镜像的压缩及原始长度、SHA-256、只读挂载和启动配置检查通过。
- 两份根文件系统检查了 root 锁定、空 machine-id、无预置账户密钥/SSH host keys、无首启完成标记及列明的网络配置和历史残留；此检查不是通用秘密扫描器。
- 镜像内六个 Python 源文件与清单一致；清单中的九个生产源码、配置和锁定输入文件与交付源码匹配。
- 最终 rc2 eMMC rootfs 的隔离副本通过 3 项 chroot 集成检查：真实账户创建、权限与 root 锁定、SSH host keys、公钥 SSH 登录及 `sudo -n true`。

chroot 检查替代了 hostnamectl，不能单独证明 systemd 开机顺序；后者由下述真机启动验证。

## 真机安装与启动

- 真实设备探测、设备绑定计划、MMC/CID 门禁通过。user area（7,818,182,656 字节）及 boot0/boot1 全量备份完成，Mac 独立完整解压检查 CRC、长度和 SHA-256；前 700 MiB 与两个 boot area 匹配安装前基线。
- 生产安装器写入 rc2 eMMC 载荷并读回校验；根分区扩容至 6,547,308,544 字节，随机 UUID 贯穿 fstab、uEnv 和启动清单。前 700 MiB、boot0/boot1、分区间隙保持不变；10 个启动 bundle 文件验证通过。
- 安装运行环境为 rc1 USB。rc1 与 rc2 镜像的 boot.py、firstboot.py、installer.py、maintenance.py 四个关键源码快照哈希相同；最终 rc2 USB 后续单独验证，未再次执行 eMMC 安装。
- 最终 rc2 USB 写入及镜像覆盖范围完整读回通过：4,836,032,512 字节的 SHA-256 与发行镜像一致；加入私有公钥配置后安全卸载并通过 FAT 检查。未声明读回物理 U 盘的其余空间。
- 原版 USB 首次物理冷启动在内核早期停住，未进入 /init；热重启成功。加入临时 initcall 日志的物理冷启动成功，但该参数改变了启动时序，不能当成修复。
- 恢复原始 uEnv 并逐字节校验后，原版参数连续两次 USB 物理冷启动复测通过。失败记录保留，根因未确定。
- 最终明确拔除 USB 并物理断电后，eMMC 启动通过，根 UUID 与安装结果一致。两次 USB 复测和最终 eMMC 启动均通过公钥 SSH、sudo、介质身份、失败服务为 0 与控制台启动顺序检查；用户确认 HDMI 登录提示。
- 此前名为 emmc-cold2 的记录只保留为第二次 eMMC 根系统观测，撤回“拔 USB 物理冷启动”分类。未枚举 USB 和 vendor 的 cold_boot 字样都不能单独证明用户的物理操作。HDMI 本地键盘登录未测试。

rc1 首次 USB warm boot 暴露 console-setup 临时文件被启动时 /tmp 清理删除的竞态。
rc2 增加 After=systemd-tmpfiles-setup.service；上述成功启动均确认清理结束后才运行
console-setup，服务成功。原 rc1 失败记录保留，不以事后重启服务结果覆盖。

## 未覆盖和已知问题

- USB 曾发生一次内核早期卡住，原因未确定。后续复测通过不构成故障修复或长期启动可靠性证明，因此本版仍为候选版。
- Bluetooth 初始化实测出现 BCM baudrate/reset 超时 -110，未通过；Wi-Fi、音频及长期运行未验证。
- 完整备份已验证，但实际备份还原、安装中断和断电恢复未测试。
- 原厂 Android 的 vendor multiboot 首次激活不在本流程；本样机此前已激活。
- --initialize-layout 的真实分区重读未覆盖，本次使用已有布局；该分支只有单元检查。
- 本地键盘首启和登录、不同安装间 UUID 的统计唯一性及任意中断点恢复保证未覆盖。

tests/integration_installer.py 的 loop 测试会模拟硬件探测、boot0/boot1 和部分系统操作。
rc1 曾通过该完整流程；rc2 发行目录没有 installer-loop.txt，不把历史 loop 结果称为
rc2 最终载荷测试。rc2 的真实安装证据见上述实机报告。

只读采集可运行 sudo python3 tests/collect_hardware.py。工具省略常见网络/设备身份与密钥，
文件系统 UUID 使用指纹，但自定义挂载路径等仍应审阅；不要直接公开完整采集器输出、
原始串口日志、设备绑定计划或备份清单。发行报告只摘取经过审阅的字段。

## 构建与发布边界

样机后续的 GPU、4K 视频与 HDMI 音频实验单独记录于[多媒体实测](MULTIMEDIA.md)。
这些测试安装了额外用户空间软件，并使用实验 CMA 设备树；不能追溯算作原 rc2 镜像的默认功能。

输入镜像、U-Boot 资产和容器 digest 已固定，启动脚本生成是确定性的；完整 ext4/VFAT
镜像的位级复现未证明，基础系统与内核来自上游二进制。P212 dirty U-Boot 的精确源码
仍待补齐，见第三方来源说明。原创工具采用 MIT；第三方组件保留各自许可证。
当前发行物未签名、未公开发布；哈希仅证明文件一致性。
