# M17S 多媒体实验源码

这里保留真机实验代码，不作为 `0.1.0rc2` 镜像的预装播放器。

## HDR10 到 SDR

`hdr-sdr/` 包含：

- `play_hdr_sdr.py`：HEVC V4L2 解码、dmabuf、GstGL shader、显示和可选音频的完整实验管线。
- `tonemap.frag`：PQ EOTF、Reinhard、BT.2020 到 BT.709、gamma 2.2。
- `glpipe_probe.py`、`gl_stage_probe.py`、`rate_probe.py`：真实 VDEC 与纯 GPU 分级吞吐探针。
- `frame_stats.py`、`capture_frame.py`：帧统计和 NV12 抓取。
- `dmabuf_probe.py`、`dmabuf_pool_probe.py`、`vdec_meta_probe.py`、`vdec_layout.py`：VDEC/dmabuf 边界调查。
- `m17s_tonemap.c`、`m17s_tonemap_fast.c`：CPU 参考实现和 A53 优化版本。
- `reference_render.py`、`compare_c_py.py`、`tonemap_test.py`：独立数学参考、逐像素比对和性能驱动。

当前结论和复现边界见 `../docs/HDR_SDR.md`。

## 音频

`audio/` 包含固定 TrueHD/DTS/AC-3 播放探针以及 `playbin3`、legacy `playbin`、`input-selector` 三种动态切轨实验。固定音轨已验证；动态切轨仍保留为失败样本，不能作为播放器实现。

## 数据边界

原始电影、提取音轨、NV12 帧、播放器日志、内核日志和设备网络配置均不进入仓库。脚本默认使用板端 `/var/tmp` 路径，运行前应按实际测试文件调整参数。
