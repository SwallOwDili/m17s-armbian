# 第三方来源与许可证

本项目的 `LICENSE`（MIT）只覆盖本仓库原创脚本和文档。基础 Armbian 镜像、Linux
内核、设备树、固件和 Debian 软件包分别保留各自上游许可证；重新分发时应随相应
发行物保留其版权和许可证文本。

主要来源：

- [ophub/amlogic-s9xxx-armbian](https://github.com/ophub/amlogic-s9xxx-armbian)：基础 Armbian 镜像及 Amlogic 启动布局。
- [ophub/u-boot](https://github.com/ophub/u-boot)：P212 启动二进制资产的上游仓库；锁定提交见 `sources.lock.json`。该提交用于资产来源和校验，并非已确认的精确构建源码。
- [Armbian](https://github.com/armbian/build)：Armbian 构建系统及其发行组件来源。
- [Debian](https://www.debian.org/intro/free)：Bookworm 用户空间及 Debian 软件包的许可证信息。

`sources.lock.json` 固定记录了基础镜像、P212 U-Boot 和 builder image 的 URL、SHA-256、
大小或内容地址，以及 U-Boot 对应的仓库提交。相同输入、相同锁定资产和相同构建流程
可用于重跑构建流程；这些记录本身不证明当前输出字节与某次历史输出逐字节相同。

FAT 分区中保留的 `multiboot-activation/` 脚本取自该锁定基础镜像，未自动执行。
其独立源码出处及完整许可材料仍属于公开二进制再分发前需要补齐的来源清单。

P212 `u-boot-p212.bin` 的来源资产和提交已锁定，但对应的 **2021.04 dirty build exact
source 尚未定位**，因此本项目不声称整个系统或该二进制可以由公开源码逐字节重建。
在公开发布包含的二进制前，还需要补齐完整来源清单、上游许可证和对应资产校验记录。
