# 多媒体实机验证

测试日期：2026-09-21—22。对象：一台 M17S / S905X / 2 GiB 样机，Linux `6.12.109-ophub`。

**真实 4K 电影片段已能流畅播放，并从主板模拟音频口出声。** 用户确认“是电影画面，运动流畅”，启用模拟音频后确认“有声音，和画面基本同步”。常驻声卡配合 0.75 秒淡入淡出后，真实电影开头和结尾均无异响。当前输出为 1080p；HDR→SDR shader 与 dmabuf 路径已得到可见效果但尚未完成长片和内核稳定性验收，实验性多音轨切换测试出现错误，因此**完整播放器及其验收尚未完成**。

## 配置与发行物边界

- 在 rc2 样机上另行安装 Mesa 22.3.6、GStreamer 1.22、Weston 10、glmark2 及测试依赖；原 rc2 server 镜像未重建，也没有预装完整播放器环境。
- 内核、initrd 与系统根分区身份保持不变。通过独立启动槽位，仅将 DTB 的 CMA 大小从 256 MiB 调为 **768 MiB**；启动后实测 `CmaTotal=786432 KiB`。原始 DTB 和入口备份保留，现有发行文件未修改。
- 768 MiB 是本次 HEVC Main10 实验配置，尚未完成内存压力和启动可靠性回归。CMA 未被设备占用的可迁移页可供系统使用，并非永久从应用内存扣除相同容量。
- 实验期间一次热重启停在内核早期；用户实际断 DC 电源后恢复。原因未确定，不能把实验配置视为已解决启动稳定性。

## 结果

| 项目 | 实测 | 判定边界 |
| --- | --- | --- |
| GPU / GLES | Mesa Lima 识别 Mali450；glmark2 的 build、texture 两场景约 60 fps；Weston simple-egl 约 60 fps | 基础渲染可用；不是完整桌面、浏览器或全套 glmark2 成绩 |
| H.264 4K30 | 合成片主体流畅，用户曾确认可见；三轮各提交 299/300 帧 | 部分通过，EOS 尾帧未清空 |
| HEVC Main10 4K 电影 | V4L2 硬解 + DMA-BUF + Weston/Wayland，用户确认电影画面流畅 | 当前 1080p 输出、约 45.5 秒片段通过；不代表原生 4K 输出或完整影片 |
| 主板模拟音频 | PCM 及原 TrueHD 音轨复播均确认有声；设置已通过断电恢复检查 | 常驻声卡与 0.75 秒淡入淡出后，实际电影开头和结尾无爆音；服务首次启动仍有一次上电瞬态 |
| 原片音轨解码 | 盒子原生解码 TrueHD / DTS-HD MA 为八声道 PCM，AC-3 为六声道 PCM，再下混立体声 | 三种固定音轨的 45 秒片段均完成；不代表 Atmos 渲染、直通或多声道物理输出 |
| 多音轨切换 | 已做实验性测试：TrueHD → DTS-HD → 法语 AC-3 → 英语 AC-3 | 测试未通过：通用播放接口报 not-linked，统一 PCM 路径仍有时间进度落后；播放器级实现尚未完成 |
| HDMI 音频 | 系统音频路由可启用，测试时音箱实际接主板模拟口 | 未做 HDMI 输出的听感验收 |
| HDR / 色彩 | dmabuf + GstGL shader 已执行 PQ EOTF、Reinhard、BT.2020→BT.709 和 gamma 2.2；真实链路约 20–23 fps | 已得到可见效果；长片、重复启停、CMA 回收、内核稳定性和画面主观验收仍未完成 |
| VP9 4K | 曾分配失败，并在后续测试中进入不可中断 D 状态 | 未通过，768 MiB 下尚未独立复验 |

帧数、耗时和用户观察的脱敏摘录见 [multimedia-results.json](multimedia-results.json)。原始串口、系统日志和影片文件留在私有测试目录，不纳入公开仓库。

## 真实电影的测试方法

样本为《太空旅客》约 45.5 秒的水体、反光和运动场景：HEVC Main10、3840×2160、24000/1001 fps，视频约 **62.95 Mbit/s**，原片为 BT.2020/PQ HDR10。视频仅抽取、重新封装，未降低分辨率或重新编码。

软件实际解码基准为 **1088 帧**，容器内有 1090 个视频包；不能把包数当作可显示帧数。最初直接 KMS 路径四轮均输出、提交 1088 帧，sink 统计丢帧为 0，但用户看不到画面。隐藏控制台后，DRM 已显示 primary framebuffer 为 0、视频 overlay framebuffer 非零，用户仍确认黑屏。因此，计数完整不能单独证明屏幕播放成功。

改用 **Weston + `waylandsink fullscreen=true`** 后，用户确认电影画面流畅。随后加入立体声 PCM，同一 GStreamer pipeline 同步音视频；三轮均输出并提交 1088 帧、sink 统计丢帧为 0，播放阶段约 45.38 秒。直接 KMS 黑屏尚未找到根因，Weston 路径是本次验证有效的显示方式。

GPU 渲染使用 Lima，视频解码使用 `meson-vdec`，显示使用 `meson-drm`。它们是不同环节；原厂 Android 可播放 4K，不能直接证明当前 Linux 驱动组合也能完整支持。

## 模拟音频口

本次音箱接的是**主板音频口**。此前模拟路由为 DISABLED、输出开关关闭、ACODEC 音量为 0。启用后用户确认有声、音画基本同步。设置通过 `alsactl store` 保存后，已在用户实际断 DC 电源再上电后核对：音量仍为 200/255、开关保持开启、ACODEC 来源仍为 I2S；随后的原音轨复播也确认有声。

样机上使用的设置如下，声卡和控制项名称须先以 `aplay -l`、`amixer -c 0 contents` 核对：

```sh
sudo amixer -c 0 cset name='ACODEC Playback Volume' 200,200
sudo amixer -c 0 cset name='ACODEC Playback Switch' on
sudo amixer -c 0 sset 'AIU ACODEC SRC' I2S
sudo amixer -c 0 sset 'AIU ACODEC OUT EN' on
sudo alsactl store 0
```

这里 200/255 对应约 −20.55 dB，并不是最大音量。原片 TrueHD/Atmos 八声道音轨先在电脑上解码、下混为 48 kHz、16-bit、双声道 PCM，再由盒子的 `wavparse → alsasink` 播放。音频按原片起始时间设置约 42 ms 偏移。上述预解码 PCM 是最初基线；后续盒子原生音轨测试见下节，仍不代表 Atmos 渲染、直通或多声道物理输出。

## 原音轨与切换补充测试

从同一原片、同一起点无重编码抽取 11 条压缩音轨，在盒子上使用 GStreamer libav 解码；视频仍是原始 HEVC Main10 4K 片段。固定音轨各测试一轮，结果如下：

| 原音轨 | 实际解码器及输出 | 视频提交 / 解码基准 | 播放时间 | 进程 CPU（单核=100%） |
| --- | --- | --- | --- | --- |
| TrueHD，含 Atmos 元数据 | avdec_truehd，48 kHz、S32LE、8 声道 | 1088 / 1088 | 45.529 s | 106.42% |
| DTS-HD MA | avdec_dca，48 kHz、S32LE、8 声道 | 1088 / 1088 | 45.421 s | 39.32% |
| 法语 AC-3 | avdec_ac3，48 kHz、F32LE、6 声道 | 1088 / 1088 | 45.393 s | 22.41% |

三轮均收到 EOS，视频 sink 丢帧计数为 0。这里是盒子现场软件解码音频、硬件解码视频；音频最终统一下混到 48 kHz、16-bit、双声道模拟输出。DTS 测试使用 `avdec_dca` 并核对八声道解码输出，未使用默认的 DTS core 解码器。没有验收无损直通、Atmos 对象渲染或 7.1 音箱。

TrueHD 另有三轮循环，用户确认“有电影画面，也有声音”；不能据此补写原音轨已精确音画同步。用户随后指出**画面偏暗、音频开头和结束有异响**。音频启停问题已通过下述常驻服务规避，并由实际电影复测确认；当前 HDR 路径仍没有正确色调映射。

切轨分别测试了 `playbin3`、`playbin` 和四条音轨预先统一成 PCM 后的 `input-selector`。前两者在 TrueHD→DTS 切换时报 `not-linked`；后一种虽确认四个目标 stream ID 和音频 buffer 都到达 ALSA sink，但运行约 65 秒后音频 PTS 只到 36.186 秒，未收到正常 EOS。**多音轨切换未通过**，不将“选择属性已改变”或“视频帧完整”当成切轨成功。

### 启停爆音定位

在数字静音下打开模拟 PCM 设备，用户仍听到两次“啪”声；先关闭 ACODEC 输出再打开声卡、或把 ACODEC 音量置零后再分级恢复，只能降到一次。这说明至少有一次瞬态来自模拟 DAC / 功放进入工作状态，单靠音频样本淡入不能消除。

实验方案使用 ALSA `dmix` 常驻播放数字静音，让播放器通过 `m17s_dmix` 混音，从而避免每次任务都关闭并重开模拟链路。独立第三段测试音的启停无爆音；随后实际电影使用 TrueHD 原音轨、`m17s_dmix` 和首尾各 0.75 秒淡入淡出，用户确认开头和结尾均无“啪”声。该轮收到 EOS，视频解码及渲染均为 1088 帧、sink 丢帧为 0，播放 45.384 秒。

服务启动时首次给模拟链路上电仍会响一次，不能写成硬件爆音已修复；它把问题从“每次播放两端都响”收敛为“服务首次启动一次”。文件和安装说明见[音频常驻服务](../extras/audio-keeper/README.md)。

### HDR→SDR 性能试验

当前连接显示器的 256-byte EDID 含 CTA 扩展，但没有 HDR Static Metadata Data Block，因此 PQ 内容需要真正转换为 SDR，不能只改颜色标签。

后续分级实测纠正了早期结论：带 `gldownload` 的约 9.5 fps 是 CPU 回读瓶颈，不是 Mali-450 的 shader 上限。纯 GPU 4K 单 pass 约 42.21 fps，三 pass 约 32.38 fps。真实 VDEC→GL 链路使用 `capture-io-mode=mmap` 时约 0.5 fps，切换到普通 `dmabuf` 后达到约 20–23 fps；`sync=false` 吞吐探针达到约 54–60 fps，同步显示约 23.85 fps。

PQ EOTF、Reinhard、BT.2020→BT.709 和 gamma 2.2 shader 已得到可见效果，但长片、重复启停、CMA 回收和 `meson_vdec` Oops 尚未通过发布验收。完整数据、音频影响和复现命令见 [HDR10 到 SDR 实验状态](HDR_SDR.md)，源码见 `experiments/hdr-sdr/`。

## 保留的问题

- **HDR 路径尚未发布验收。** 普通 `dmabuf` 已打通真实 VDEC→GstGL shader，不能再把早期 mmap/CPU 回读性能当作 GPU 极限；但重复启停曾触发 CMA 枯竭和 `meson_vdec` Oops，完整片源、长期稳定性及画面主观标准仍待验证。
- **警告没有清零。** 片段从 CRA 开放 GOP 开始，开头出现缺少 POC 123 参考帧及 QoS 警告，更像截取边界影响；EOS 仍报告一帧未排空和时间戳倒退。即使总输出数与软件基准相同，也不能写成全链路零错误或逐像素正确。
- **默认内存配置不足。** CMA 256 MiB 下 HEVC 缓冲和 VP9 workspace 分配失败；384 MiB 下 HEVC 仍失败，随后 VP9 清理卡在 `codec_vp9_flush_output`。前序失败可能影响后续状态，不能把残留日志全部归给 VP9。
- **长时间与交互未测。** 完整影片、持续数十分钟、seek、暂停恢复、浏览器、复杂桌面、并发内存压力以及原生 4K HDMI 输出仍待验证。

测试程序的帧数及 GStreamer bus 统计无法覆盖 stderr、内核警告和物理显示；退出成功或统计通过均不等于上述问题已消失。

## 参考

- [S905X 数据手册](https://www.scs.stanford.edu/~zyedidia/docs/amlogic/s905x.pdf)：芯片能力上限，不代表当前系统验收结果。
- [Mesa Lima](https://docs.mesa3d.org/drivers/lima.html)：Mali-450 GPU 驱动。
- [GStreamer 1.22 KMS sink 源码](https://github.com/GStreamer/gstreamer/blob/1.22.0/subprojects/gst-plugins-bad/sys/kms/gstkmssink.c)：直接 KMS 显示与 DMABUF 导入路径。
