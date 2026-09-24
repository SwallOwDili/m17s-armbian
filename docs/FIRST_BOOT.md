# 首次启动配置

首次启动服务在 `ssh.service` 和 `getty@tty1.service` 之前运行。它只在
`/etc/m17s-armbian/provisioned` 不存在时执行；只有所有步骤完成后才会原子创建该标记。
因此失败后重启可以安全重试，服务不会提前放行 SSH。

把镜像写入 U 盘后、盒子启动前，在 U 盘 FAT 分区根目录创建 `firstboot.json`
（盒子启动后该分区挂载在 `/boot`）：

```json
{
  "hostname": "m17s-box",
  "username": "m17s",
  "ssh_authorized_keys": ["ssh-ed25519 AAAA... comment"]
}
```

上面的公钥只是格式占位符，不能直接使用；必须替换为自己的完整 OpenSSH 公钥内容。

配置不接受密码字段。`ssh_authorized_keys` 只接受 `ssh-ed25519`、`ssh-rsa` 和
`ecdsa-sha2-nistp256` 公钥，且不接受 authorized_keys 选项或私钥。带公钥的
key-only 账户加入 `sudo` 组并获得 `NOPASSWD: ALL`，所以必须只放入可信公钥。

没有配置文件时，服务在本地 HDMI 键盘连接的 tty1 上询问主机名、用户名和两次密码。
密码至少 12 个字符，不能包含冒号、回车或换行。root 账户会被锁定。

## README 首启摘要

首次启动前可在 `/boot/firstboot.json` 写入主机名、用户名和 OpenSSH 公钥；不写配置文件
则使用 HDMI 键盘在 tty1 交互设置。首启服务在 SSH 前运行，只有账户、sudo、SSH 主机
密钥和公钥配置全部成功后才创建完成标记；失败可重启重试，系统不会接管已有同名账户。
