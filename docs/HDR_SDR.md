# HDR10 到 SDR 实验状态

测试对象：M17S，Amlogic S905X，Mali-450/lima 22.3.6，GStreamer 1.22，Weston/Wayland，CMA 768 MiB 实验设备树。

## 当前结果

实验管线已经跑通以下计算：

```text
HEVC Main10 4K
  -> meson-vdec / NV12
  -> dmabuf
  -> GstGL
  -> PQ EOTF
  -> Reinhard tone mapping
  -> BT.2020 to BT.709
  -> gamma 2.2
  -> GL sink / 1080p display
```

真实 45.5 秒片段为 3840×2160、24000/1001 fps、约 62.95 Mbit/s、BT.2020/PQ。当前显示器只覆盖 1920×1080@60。

## 决定性数据

| 路径 | 实测 |
| --- | --- |
| 纯 GPU，4K 单 shader pass | 约 42.21 fps |
| 纯 GPU，4K 三 shader pass | 约 32.38 fps |
| VDEC→GL，`capture-io-mode=mmap` | 约 0.5 fps |
| VDEC→GL，`capture-io-mode=dmabuf` | 约 20–23 fps |
| dmabuf 全链，`sync=false` 吞吐探针 | 约 54–60 fps |
| dmabuf 全链，同步显示 | 约 23.85 fps，两次丢帧 |
| 视频 + `m17s_dmix` | 约 4 fps |
| 视频 + 直连 `hw:0,0` | 约 22.5–23.25 fps |

原先把约 9.5 fps 解释成 Mali-450 极限是错误结论；那组数据包含 `gldownload` 的 CPU 回读。纯 GPU 测量证明 shader 算力有余量，主要问题位于缓冲传递、同步和音频链路。

`capture-io-mode=dmabuf-import` 仍会报 `No downstream pool to import from` 或 `Failed to allocate required memory`。当前可用路径是普通 `dmabuf`，不是 `dmabuf-import`。

## 音频

- DTS：`dcaparse ! avdec_dca`。
- AC-3：`ac3parse ! avdec_ac3`。
- `m17s_dmix` 常驻链路会显著拖慢视频实验。
- 停止 `m17s-audio-keeper` 并使用 `alsasink device=hw:0,0` 后，视频恢复到接近片源帧率。
- 直连设备只能由一个进程占用；keeper 未停止时会出现 `Device 'hw:0,0' is busy`。

## 内核与 CMA

重复启动、异常终止或循环播放曾触发：

```text
Unable to handle kernel paging request
codec_hevc_process_segment+0x454/0xe6c [meson_vdec]
```

CMA 空闲量也可能从约 762 MiB 降到 65–160 MiB，随后出现 `Buffer pool activation failed`。污染状态下的性能数据不作结论；完整重启后再测。

每轮前后至少记录：

```sh
grep -E 'CmaTotal|CmaFree' /proc/meminfo
sudo dmesg | grep -E 'Internal error|Unable to handle|codec_hevc' | tail -20
```

管线应正常进入 `NULL`，避免用强制信号中断正在工作的 V4L2 解码器。

## 复现

```sh
export XDG_RUNTIME_DIR=/run/m17s-media-weston
export WAYLAND_DISPLAY=m17s-media
export GST_GL_PLATFORM=egl
export GST_GL_WINDOW=wayland
export GST_GL_API=gles2

sudo -E python3 experiments/hdr-sdr/play_hdr_sdr.py \
  --file /var/tmp/SAMPLE.mkv \
  --shader experiments/hdr-sdr/tonemap.frag \
  --io-mode dmabuf
```

带独立 DTS 音轨：

```sh
sudo systemctl stop m17s-audio-keeper
sudo -E python3 experiments/hdr-sdr/play_hdr_sdr.py \
  --file /var/tmp/SAMPLE-video.mkv \
  --audio-file /var/tmp/SAMPLE-audio.mka \
  --audio-track audio_1 \
  --audio-parser dcaparse \
  --audio-decoder avdec_dca \
  --audio-device hw:0,0 \
  --io-mode dmabuf
```

## 验收边界

当前结果证明 M17S 上存在可用的 VDEC/dmabuf/GPU 色调映射路径，并已得到可见播放效果。发布级完成仍需满足：完整片源实时播放、长时间无掉帧、停止重播后 CMA 回收、无 `meson_vdec` Oops、音频同步稳定，以及用户对亮度、肤色、黑位和高光的画面验收。
