# 泰山派 AIBox 固件

本仓库的 [GitHub Actions](.github/workflows/build-image.yml) 从 [ophub 的 lckfb-tspi Armbian 镜像](https://github.com/ophub/amlogic-s9xxx-armbian/releases/tag/Armbian_resolute_arm64_server_2026.09) 制作可刷写镜像。基底固定为 `Armbian_26.11.0_rockchip_lckfb-tspi_resolute_6.1.157_server_2026.09.01.img.gz`（SHA256 `247d8aad854c3bd0d9ba2fd755678967a6f7e59e91b89e1d331fae4f8bba01e6`），与已运行的泰山派内核和设备树一致。构建不会重新编译内核，也不引入上游后续可能变动的设备树。

镜像预装离线语音助手、SSD1306 OLED / 三键菜单 / LED PWM 功能、RKNPU SenseVoice 中文识别、CPU 备用识别、小雅中文 TTS、Ollama Qwen3 0.6B 与 1.7B。NPU 只用于语音识别。生成过程固定下载文件的 SHA256；镜像不包含 Wi-Fi 密码、SSH 私钥或板上的个人配置。

## 在现有 Armbian 上一键部署

适用于使用 `rk3566-taishanpi-v10.dtb`、Ubuntu 26.04 `resolute` 的 ARM64 泰山派；无需刷写镜像。板子联网后执行：

```sh
git clone https://github.com/JasonYANG170/tspi-AIBox.git
cd tspi-AIBox
sudo bash install.sh
```

脚本安装缺失依赖与模型、运行应用测试、启用并启动 `ollama` 和 `ai` 服务。下载文件逐一校验 SHA256；再次运行会复用已有模型。现有的 `/var/lib/eda-aibox/settings.json`、`led.json`、ASR/TTS 后端选择及 Ollama 模型都会保留；更新前的代码和服务文件保存在 `/opt/eda-aibox/backups/`。若首次启用 LED 覆盖层，重启后硬件 PWM 才会生效。部署后检查：

```sh
systemctl status ai ollama
journalctl -u ai -n 50 --no-pager
```

## CI 构建与刷写

在仓库的 **Actions → Build TaishanPi image → Run workflow** 手动运行，或向 `main` 推送相关文件。成功后下载 `tspi-aibox-armbian-6.1.157` artifact，解压其中的 `.img.zst`，再执行：

```sh
sha256sum -c tspi-AIBox_lckfb-tspi_resolute_6.1.157_YYYYMMDD.img.zst.sha256
zstd -d tspi-AIBox_lckfb-tspi_resolute_6.1.157_YYYYMMDD.img.zst
# 核对目标设备路径后刷写；本命令会覆盖整张卡
sudo dd if=tspi-AIBox_lckfb-tspi_resolute_6.1.157_YYYYMMDD.img of=/dev/目标设备 bs=4M status=progress conv=fsync
```

镜像分区扩至 12 GiB，因此目标存储至少需要 12 GiB。初次启动按 Armbian 原版流程在串口设置账户、配置网络；助手使用已预建的 `yang` 系统账户，服务会在音频设备就绪后启动。进入系统后检查：

```sh
systemctl status ai ollama
journalctl -u ai -b --no-pager | tail -50
cat /sys/kernel/debug/rknpu/load
```

当前程序假定声卡为 `plughw:1,0`，OLED 位于 I2C-2 地址 `0x3c`，AHT10 位于 I2C-2 地址 `0x38`，按键在 GPIO3_A1/A2/A3。AHT10 在已有板上曾出现读数失败；程序会提示传感器故障。首次启动仍需按实际接线核对外设。详细功能、性能与限制见 [部署说明](DEPLOYMENT.md)。

## 本地构建

在带 loop 设备与 root 权限的 ARM64 Ubuntu 26.04 上运行：

```sh
sudo apt-get install gdisk parted e2fsprogs zstd bzip2 curl device-tree-compiler
sudo bash scripts/build-image.sh
```

输出放在 `out/`，下载缓存放在 `.cache/image-build/`。构建脚本会校验基底、运行不依赖外设的程序测试、验证两个 Ollama 模型，并生成镜像 SHA256。

预训练模型来自各自上游，使用时应核对其许可证；小雅模型卡注明训练数据用于非商业场景。
