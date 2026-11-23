# AGENTS.md — as7343-lightmeter

基于 AS7343 多光谱传感器的室内光源质量测试程序，运行在树莓派（I2C1, 0x39）。

## 目录约定

| 路径 | 内容 |
| --- | --- |
| `as7343.py` | 芯片驱动：寄存器读写、光谱测量、FD 闪烁采样。只放和芯片相关的逻辑 |
| `lightmeter.py` | 命令行入口与全部算法（光谱重建、色度、显色、闪烁、评价） |
| `webui.py` | Web 服务（标准库 http.server + SSE），独占传感器线程，只做调度和 HTTP，不放算法 |
| `web/index.html` | 手机端页面，单文件（HTML/CSS/JS 内联），不引第三方前端库 |
| `web/preview.png` | 页面截图，README 引用；页面改版后同步更新 |
| `data/records.jsonl` | Web 端「记录本点」的测量记录，运行时生成，不进 git、不随部署覆盖 |
| `tools/as7343-web.service` | systemd 单元模板，安装到 Pi 的 `/etc/systemd/system/` |
| `cal/ams_golden.json` | ams Golden Device 标定矩阵，**由 `tools/extract_ams_cal.py` 生成，不手改、不进 git**（ams 资料不可转发） |
| `cal/user.json` | 用户现场标定（照度系数），运行时生成，不进 git |
| `tools/` | 标定提取、部署脚本、systemd 单元 |
| `tests/` | 离线测试，不需要硬件；缺标定文件时相关用例自动跳过 |
| `docs/` | 设计说明与调试记录 |

## 编码规范

- Python 3.11，tab 缩进（按 4 空格计），行长 ≤ 120，推荐 ≤ 80
- 依赖只允许：numpy、smbus2、colour-science（见 `requirements.txt`）
- 寄存器地址、位定义必须能在数据手册 DS001046 v6-00 里找到出处
- 不提交个人环境信息（IP、密码、Wi-Fi 名）和 ams 的受版权保护的资料

## 验证

改完必须跑（colour-science 需要 Python 3.11+，一般在 Pi 的 venv 里跑）：

```sh
.venv/bin/python -m unittest discover -s tests
PI=pi@<host> tools/deploy.sh                       # 同步到 Pi
ssh pi@<host> 'cd ~/as7343-lightmeter && .venv/bin/python lightmeter.py report'
```
