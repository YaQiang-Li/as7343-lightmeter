# as7343-lightmeter

用 ams-OSRAM AS7343 多光谱传感器 + 树莓派做的室内光源质量测试仪。手机浏览器实时看结果，可以拿着设备在全屋逐点测量。

*Indoor light quality meter (illuminance, CCT, Duv, CRI, TM-30, flicker) built on the AS7343 spectral sensor and a
Raspberry Pi, with a live phone-friendly web UI.*

| 类别 | 指标 |
| --- | --- |
| 光度/色度 | 照度 lx、CCT（Ohno 2013）、Duv、CIE 1931 x,y、CIE 1976 u'v' |
| 显色性 | CRI Ra、R1–R14（含 R9）、TM-30-18 Rf / Rg |
| 频闪 | 基频、波动深度 Mod%（percent flicker）、频闪指数 FI、IEEE 1789 风险分级 |
| 光谱 | 380–780 nm 重建光谱 |

## 硬件

- 树莓派，在 Raspberry Pi 4 Model B / Raspberry Pi OS bookworm (64-bit) 上开发测试
- AS7343 模块（I2C 地址 0x39），接 40pin：

| 模块 | 树莓派引脚 |
| --- | --- |
| VCC | 3.3V（pin 1）— 以模块标注为准 |
| GND | GND（pin 6） |
| SDA | GPIO2 / SDA1（pin 3） |
| SCL | GPIO3 / SCL1（pin 5） |

## 安装

1. **开启 I2C**：`sudo raspi-config` → Interface Options → I2C，或在 `/boot/firmware/config.txt` 里加
   `dtparam=i2c_arm=on` 后重启。
2. **复制代码并装依赖**（Python 3.11+）：

   ```sh
   git clone <this repo> ~/as7343-lightmeter && cd ~/as7343-lightmeter
   python3 -m venv --system-site-packages .venv
   .venv/bin/pip install -r requirements.txt
   ```

3. **生成标定文件 `cal/ams_golden.json`（必需）**。光谱重建矩阵来自 ams 的评估软件包。ams 资料的版权声明不允许转发，
   所以仓库里不带这个文件，要从你自己下载的 ams-OSRAM **AS7343 EvalSW ALS** 包（开发时用的是 v2-4-3）里提取：

   ```sh
   unzip -j AS7343_EvalSW_ALS_v2-4-3.zip '*Spectral to XYZ sensor calibration ALS woCM v2_2.xlsm'
   python3 tools/extract_ams_cal.py 'AS7343_AS7352 Template Spectral to Spectral to XYZ sensor calibration ALS woCM v2_2.xlsm'
   ```

4. **验证**：

   ```sh
   .venv/bin/python -m unittest discover -s tests     # 离线测试，不需要传感器
   .venv/bin/python lightmeter.py report               # 读一次真实数据
   ```

## 使用

### 手机实时页面

```sh
.venv/bin/python webui.py --port 8080
```

手机连同一个局域网，打开 `http://<树莓派IP>:8080`（或者 `http://raspberrypi.local:8080`，需要手机支持 mDNS）。

- 页面顶部：照度大字号、目标照度（保存在手机浏览器里）、光色色块、最近 60 秒照度曲线
- 卡片：色温、Duv、Ra、R9、TM-30，按判定阈值标绿/红；频闪点「测一次」，约 4 秒出结果
- 刷新速度：照度、色温、光谱约 3 次/秒；Ra/R9/TM-30 约 1 次/秒
- 全屋记录：填位置后点「记录本点」，约 8 秒（8 帧平均 + 频闪）。结果在表格里逐点对比，可导出 CSV，
  数据存在 `data/records.jsonl`
- 测量时手机离传感器远一点，手机屏幕的光也会被测进去

开机自启（单元文件默认用户 `pi`、目录 `/home/pi/as7343-lightmeter`，不同的话先改）：

```sh
sudo cp tools/as7343-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now as7343-web
journalctl -u as7343-web -f
```

Web 服务会独占传感器，跑命令行前先 `sudo systemctl stop as7343-web`。

### 命令行

```sh
.venv/bin/python lightmeter.py report                     # 完整报告（约 9 秒）
.venv/bin/python lightmeter.py report --target-lux 500 --out r.json
.venv/bin/python lightmeter.py monitor                    # 连续显示照度/色温，Ctrl-C 退出
.venv/bin/python lightmeter.py calibrate --ref-lux 512    # 对照照度计读数校正照度
```

### 测量方法

- 评估房间照明是否够亮：传感器水平朝上，放在工作面（书桌、操作台）上
- 评估灯具本身（色温、显色、频闪）：传感器正对灯具
- 人和其他遮挡物不要挡在传感器和光源之间

## 原理

**光谱/色度**：采用 18 通道自动 SMUX。积分时间固定 100 ms，正好是 100 Hz 和 120 Hz 纹波周期的整数倍，可以抵消工频纹波带来的读数跳动。
增益在 0.5x–512x 之间自动调整。按 ams 的约定计算 Basic Counts = raw / (gain × t_ms) × 增益校正，再乘 ams Golden Device 光谱重建矩阵
（12 通道 → 300–1100 nm，1 nm 步长）得到光谱，用 CIE 1931 2° CMF 算出 XYZ，照度 = 683·Y。
报告里的「XYZ 直接法」用的是 ams 模板中的 XYZ 矩阵，作为交叉校验。`tests/` 用模板自带的演示数据复现了模板的计算结果（XYZ 误差 < 0.5%）。

**显色性**：在重建光谱上调用 [colour-science](https://www.colour-science.org/) 计算 CIE 13.3 CRI 和 ANSI/IES TM-30-18。

**频闪**：开启 FDEN 后，FD 光电二极管以 1/((FD_TIME+1)×2.78 µs) 的速率把 8 位采样写入 FIFO。
取 FD_TIME=255，采样率 1405 Hz，采集 2 s。先用 FFT 找基频，再把每 0.25 s 一段的数据按基频折叠（等效时间采样），恢复出单个周期的波形，
各段结果取中位数，得到 Mod% 和 FI。

## 精度与局限（必读）

1. **标定对象不同**：ams 矩阵是针对它自家 EVK 参考板（带扩散片）标定的，普通模块的光学结构不同，照度会有系统偏差，
   可以用 `calibrate` 对照照度计修正。色度只依赖通道之间的比例，受影响较小，但没有和分光辐射计比对过，绝对误差未知。
2. **CRI/TM-30 是估算值**：12 个通道重建出的光谱是平滑的，LED 的窄峰会被抹平，Ra/R9 往往偏高。
   适合横向比较灯具，不能当作分光辐射计的测量结果。
3. **显示器等窄峰光源**：重建误差更大，读数只能用来比较调整前后的变化。
4. **频闪频率上限**：基频只能可靠测到约 700 Hz。更高频的 PWM 会混叠到较低的表观频率上，这时测出的频率和风险等级不可信。
5. **频闪检出下限**：和信号强度有关，每次测量都会给出本次的检出下限。
6. **芯片行为（实测）**：FD_GAIN ≥ 1024x 时 FIFO 读数全为 0，所以驱动最高只用到 512x；
   SP_EN 和 FDEN 同时打开时，FD 数据不写入 FIFO，因此光谱和频闪要分开测。

## 判定阈值

「合格/不合格」用的是下面这些参考阈值，定义在 `lightmeter.judge()` 里：

| 项 | 阈值 | 说明 |
| --- | --- | --- |
| 照度 | `--target-lux` / 页面里填写 | 按场景自定 |
| CRI Ra | ≥ 80 | 室内长时间停留场所的常用要求 |
| R9 | > 0 | LED 光源的常用要求 |
| Duv | \|Duv\| ≤ 0.006 | 常用色偏容差 |
| CCT | ≤ 4000 K | 长时间停留场所的常用建议 |
| 频闪 | IEEE 1789 非高风险 | 低风险：f<90 Hz 时 Mod% ≤ 0.025f，90–1250 Hz 时 Mod% ≤ 0.08f；无影响：f<90 Hz 时 Mod% ≤ 0.01f，90–3000 Hz 时 Mod% ≤ 0.0333f |

> 这些阈值按通行做法设置，**没有逐条核对 GB 50034、IEEE Std 1789-2015 原文**。用于正式验收前，请对照标准原文确认。

## 目录

| 路径 | 内容 |
| --- | --- |
| `as7343.py` | 芯片驱动：光谱测量、FD 频闪采样 |
| `lightmeter.py` | 算法（光谱重建、色度、显色、频闪、判定）与命令行 |
| `webui.py`、`web/index.html` | Web 服务（标准库 http.server + SSE）与手机页面 |
| `tools/` | 标定提取、部署脚本、systemd 单元 |
| `tests/` | 离线测试（假传感器） |
| `docs/` | 上板调试记录 |

贡献约定见 [AGENTS.md](AGENTS.md)。

## License

[MIT](LICENSE)。ams 的数据手册、评估软件和标定数据不在本仓库内，版权归 ams-OSRAM AG 所有。
