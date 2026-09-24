# 模拟音频常驻服务（实验）

M17S 的模拟音频链路从关闭切到工作状态时，样机可听到爆音。静音数据、软件淡入和先关闭 ACODEC 输出都无法完全消除第一次上电瞬态；保持 PCM 设备打开后，后续播放无需反复给模拟链路上下电。

本目录提供一个可选服务。它以音量 0 打开 `dmix`，缓慢恢复音量，并持续播放数字静音。播放器应输出到 ALSA 设备 `m17s_dmix`，并在自身开始和结束时做短淡入淡出。样机实测首次启动仍有一次爆音；服务保持运行后，独立测试音以及使用 TrueHD 原音轨的实际电影，其开始和结束均由用户确认无爆音。此方案避免每次播放都让模拟链路上下电，不代表首次硬件瞬态已被消除。

安装：

```sh
sudo install -m 0644 asound.conf /etc/asound.conf
sudo install -m 0755 m17s-audio-keeper /usr/local/sbin/m17s-audio-keeper
sudo install -m 0644 m17s-audio-keeper.service /etc/systemd/system/m17s-audio-keeper.service
sudo usermod -aG audio "$USER"
sudo systemctl daemon-reload
sudo systemctl enable --now m17s-audio-keeper.service
```

重新登录后，普通用户即可访问声卡和服务创建的 `dmix` 共享缓冲区。验证服务保持声卡打开后，可让 GStreamer 使用 `alsasink device=m17s_dmix`。播放器还需在每个媒体开始和结束时把自身音量做约 0.75 秒淡入淡出；常驻服务负责避免硬件链路被播放器反复关闭。停止并移除服务会恢复原先按播放任务启停声卡的行为。
