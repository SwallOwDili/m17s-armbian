# M17S 项目总结

## 项目定位

本仓库只服务于天猫魔盒 M17S（Amlogic S905X）运行 Armbian。主体是 Python 3.11 构建器、首启配置、保留厂商引导区的 eMMC 安装器、启动维护工具、实机验证文档和硬件实验源码。

M17S 项目的公开交付物是 Python 源码、构建与安装工具、逆向材料、设备树和多媒体实验源码。当前源码版本为 `0.1.0rc3`；最近完成实机安装验收的是 `0.1.0rc2`，rc3 仍需重新构建镜像和执行硬件回归。

## 已完成

- 构建 USB 启动镜像、eMMC bootfs/rootfs 分区载荷与校验清单。
- 实现 `m17s-emmc` 的目标探测、布局门禁、计划绑定、完整备份、写入和读回校验。
- rc3 增加 GPT 主头/备份头双检查，并将完整 uEnv、manifest 和可选 firstboot 配置纳入激活前后逐字节读回。
- 实现 `m17s-firstboot` 公钥/交互首启以及 `m17s-boot` 启动载荷验证和受限更新。
- 真机完成 USB 冷启动、eMMC 安装、拔除 USB 后断电冷启动、HDMI 登录、公钥 SSH 和 sudo 验证。
- 确认 Mali-450/lima、meson-vdec、Weston/Wayland、模拟音频口和固定 TrueHD/DTS-HD MA/AC-3 音轨路径。
- 整理原厂 Android/U-Boot 调查记录、P212 DTS 与 CMA 实验变体。

## 启动与安装关键改动

- USB 启动：原厂环境先执行 `start_autoscript`，USB FAT 提供 `s905_autoscript`，并把 `u-boot-p212.bin` 复制为 `u-boot.ext`；主线 P212 U-Boot 再按 `uEnv.txt` 启动内核、initrd、DTB 和 USB rootfs。
- eMMC 安装：只写从 700 MiB 开始的 511 MiB FAT p1 与从 1212 MiB 开始的 ext4 p2，保留前部厂商引导区、boot0、boot1 和分区间隙。
- eMMC 启动：原厂 U-Boot 先把全部载荷装入 RAM，再跳入仅修改一个内置 `bootcmd` 的 `u-boot-m17s-ram.bin`，由预载第二阶段脚本执行 `booti`。
- 激活事务：bootfs 模板不含 `emmc_autoscript`；rootfs、bootfs、被动启动文件和受保护区域全部读回校验后，安装器最后写入该文件。

完整差分见 `docs/BOOT_CHAIN.md`。

## HDR10 到 SDR

- shader 已实现 PQ EOTF、Reinhard、BT.2020 到 BT.709 和 gamma 2.2。
- 纯 GPU 4K 单 pass 约 42 fps，三 pass 约 32 fps，Mali-450 本身具备处理余量。
- VDEC 使用 `capture-io-mode=mmap` 时真实 GL 链路约 0.5 fps；切换到 `dmabuf` 后达到约 20–23 fps。
- 无时钟节流的链路探针达到约 54–60 fps；同步显示实测约 23.85 fps并出现两次丢帧。
- `m17s_dmix` 与视频同跑时曾把吞吐拖到约 4 fps；停用 keeper、直连 `hw:0,0` 后恢复到约 22.5–23.25 fps。
- 当前属于可见效果和路径验证，尚未达到长片、反复启停、CMA 回收和零内核异常的发布验收。

完整边界见 `docs/HDR_SDR.md`，代码位于 `experiments/hdr-sdr/`。

## 已知风险

- 重复 HEVC 管线实验曾触发 `codec_hevc_process_segment` 内核 Oops。
- 异常停止后 CMA 可能从约 762 MiB 下降到 65–160 MiB，需要完整重启回收。
- 动态多音轨切换的三个实验路径均未通过；固定音轨播放已验证。
- 当前显示设备只覆盖 1920×1080@60，原生 4K HDMI 输出未验收。
- rc2 是 server 镜像；Mesa、GStreamer、Weston、CMA 768 MiB 和播放器实验环境尚未固化到新发行版。

## 目录

```text
src/m17s_armbian/       镜像构建、首启、安装和维护代码
tests/                   单元测试与隔离集成测试
profiles/                M17S 板卡配置
reverse/                 U-Boot/Android 调查和 DTS 逆向材料
experiments/hdr-sdr/     HDR10 到 SDR 实验源码
experiments/audio/       固定音轨与动态切轨探针
extras/audio-keeper/     模拟音频常驻服务
docs/                    安装、兼容性、验证和多媒体文档
assets/                  主板照片、串口接线图和服务文件
```

## 下一阶段

1. 重建 rc3 三份镜像，执行 loop、USB 冷启动、完整备份、eMMC 安装、断电冷启动和回退验证。
2. 将 dmabuf、shader、音频分支和状态清理整合为播放器服务。
3. 连续播放真实长片至少 30 分钟，覆盖暂停、seek、停止重播和切轨。
4. 定位并规避 `meson_vdec` Oops 与 CMA 泄漏。
5. 在 4K 显示设备上验证原生 3840×2160 输出。
6. 将稳定的多媒体依赖、设备树和服务纳入后续镜像，重新走 USB 和 eMMC 全流程。
