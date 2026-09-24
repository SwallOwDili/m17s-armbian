# M17S 逆向材料

这里保存与 M17S 启动、设备树和原厂系统调查直接相关的材料，不混入其他设备项目文件。

## 内容

- `usb-boot-investigation.md`：原厂 Android、串口 root console、U-Boot 环境和 USB 启动链的阶段性调查。
- `dts/original.dts`：实验所用 P212 DTB 的反编译 DTS。
- `dts/cma384.dts`：只把 `linux,cma.size` 从 `0x10000000`（256 MiB）改为 `0x18000000`（384 MiB）的实验变体。
- `../tools/reverse_sshfs.py`：通过反向 SSHFS 为设备提供受控工作目录的工具。
- `../assets/m17s-board-front.jpg`：样机主板实拍。
- `../assets/m17s-uart-pads-numbered.png`：最终实测串口焊盘图。

## 边界

- `usb-boot-investigation.md` 是 USB 启动打通阶段的记录；当前 eMMC 安装流程以 `docs/INSTALLER.md` 为准。
- DTS 来自 P212 兼容配置，不表示 M17S 是晶晨 P212 开发板，也不代表所有 M17/M17S 板型通用。
- 384 MiB CMA 文件用于保留实验过程；当前多媒体实机配置采用 768 MiB，尚未作为稳定发行配置。
- 原厂固件镜像、eMMC 备份、设备 CID、MAC、SSH 材料和未脱敏日志不进入仓库。
