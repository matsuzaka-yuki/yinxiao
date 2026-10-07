# yinxiao（音效）

给 Linux 做一套「一键切换的音效预设」—— 用 PipeWire 的 filter-chain 重建 Windows 下
杜比 / DTS / Realtek 那套音效能力，21 个预设常驻，点一下切换，不重启服务、不断音。

* **21 个预设**：低音 / 人声 / 曲风 / 空间 / **厅堂卷积混响**（演唱会 / 音乐厅 / Live House / 大教堂）
* **网页曲线编辑器**：拖圆点改频率增益，实时试听，边看频响边听；黑白两套主题，削顶余量常驻可见
* **数学上永不削顶**：每个预设都算过最坏情况增益上界 σmax ≤ 1（含硬声像满量程输入）
* **装的时候零依赖**：Python 只用标准库（numpy 可选，仅加速 IR 生成）；前端是自包含单文件，
  运行期不请求任何外部资源（Tailwind 与 Preact/htm 已内联），**离线可用、不需要 node**
* **掉线自愈**：USB DAC 掉线再插回来，自动把链路和上次的音效恢复回去

```bash
git clone https://github.com/matsuzaka-yuki/yinxiao.git
cd yinxiao && bash install.sh
yinxiao 演唱会      # 切到某个预设
yinxiao edit        # 打开编辑器 http://127.0.0.1:8787
```

![编辑器](assets/editor.png)

---

## 这是什么

Linux 上一直缺一块：Windows 有 OEM 的杜比 / DTS / Realtek 音效驱动，Linux 没有对应物。
EqualizerAPO + 卷积器能拼出来，但配置零散、跨机器迁移麻烦、还容易削顶。

这里换了条路：**用 PipeWire 原生的 `libpipewire-module-filter-chain` 做成一批常驻预设**，
每个预设是一条独立的虚拟输出 + 滤波链，输出用 `node.target` 钉死在你的物理声卡上。
切换音效 = 把系统默认输出换一个 sink，**不重启任何服务、不断音、不影响正在播的歌**。

## 预设一览

| 类别 | 预设 | 说明 |
|---|---|---|
| 低音 | 超重低音 / 完美低音 / 轻低音 / 重低音炮 | 低频提振，程度与做法各不同 |
| 人声 | 清澈人声 / 磁性人声 / 贴耳人声 | 人声前凸、电台感、贴脸睡前 |
| 曲风 | 流行 / 摇滚 / 电子 / 古典 / 爵士 / 暖声黑胶 | 针对曲风的音色塑形 |
| 空间 | 3D 环绕 / HIFI 现场 / 空气感 / 等响补偿 | M/S 矩阵加宽、通透度、小音量补偿 |
| 厅堂 | **演唱会 / 音乐厅 / Live House / 大教堂** | 卷积混响，RT60 1.1 / 2.2 / 0.6 / 3.5 秒 |

厅堂预设的混响是真·卷积：`gen_irs.py` 用纯 Python 合成 4 组立体声脉冲响应
（早反射 + 三段不同衰减的尾巴 + 空气吸收），`convolver` 加载它。
干湿比可以在编辑器里实时拖，混响时间换预设即可。

## 安装

**要求**：PipeWire（含 `pipewire-pulse` 兼容层）、`python3` ≥ 3.8、systemd user、一块物理声卡。
只在 CachyOS / PipeWire 1.6.9 上实测过，其它发行版理论上通用 —— 见「已知限制」。

```bash
git clone https://github.com/matsuzaka-yuki/yinxiao.git
cd yinxiao
bash install.sh                  # 自动检测物理声卡
bash install.sh --sink <sink名>   # 也可以手动指定
bash install.sh --dry-run        # 先看它打算做什么
```

安装脚本会：检测声卡 → 生成 21 个预设 conf 与 4 组 IR → 装命令行与两个 systemd 服务
（`yinxiao-heal` 掉线自愈、`yinxiao-editor` 网页编辑器）→ 重载 PipeWire。

想开机就能用（不用登录桌面）：

```bash
sudo loginctl enable-linger $USER
```

卸载：

```bash
bash uninstall.sh            # 停服务、删 conf 与命令行，保留 ~/.local/share/yinxiao
bash uninstall.sh --purge    # 连 IR 和 hw-sink 一起删
```

## 用法

```bash
yinxiao list             # 列出全部预设（● 标出正在输出的那个）
yinxiao 演唱会            # 切换（支持 id / 中文名 / 前缀）
yinxiao next / prev      # 循环切换
yinxiao off              # 关掉音效，直通物理声卡
yinxiao status           # 看状态（设备不在、链路接错会直接告警）
yinxiao edit             # 打开网页编辑器
yinxiao fix              # 把预设输出重新接回物理声卡（幂等）
yinxiao heal             # 守护进程：DAC 掉线再上线时自动恢复
```

网页编辑器（`yinxiao edit`，`http://127.0.0.1:8787`）：

* 左侧点一下就**直接切换输出**（和 QQ音乐一样），同时进入编辑
* 拖圆点改频率/增益，滚轮改 Q，`Del` 删段；状态栏实时显示鼠标处的四条曲线读数
* 画布下方是**常驻状态栏**：preamp、σmax（混响用解析上界）、削顶余量、判定 —— 不会因为窗口窄就看不见
* 「自动归一化」勾着时，preamp 随改动自动重算 —— 怎么拖都不会削顶
* 底部按预设类型切换面板：**滤波器**（eq）/ **矩阵**（加宽、并联低音）/ **混响**（干湿比实时可调、回读 1kHz 湿/干与 RT60）
* 右上角一键切**黑白两套主题**（黑=墨黑，白=纸白）
* 保存才写盘（留一份 `.bak`），随时「回滚上次保存」

## 架构

```
  应用（浏览器 / 播放器 …）
        │  把默认输出设成某个预设
        ▼
  ┌──────────────────────────────┐
  │ effect_input.yx_<预设>        │  虚拟 sink（应用看到的就是它）
  │   ↓  一串双二阶滤波器         │
  │   ↓  preamp（自动算的防削顶）  │
  │ effect_output.yx_<预设>       │  node.target 钉死在物理声卡
  └──────────────────────────────┘
        ▼
   物理声卡
```

混响型多两步卷积：

```
  左声道 ─ EQ ─┬─────────────────────────────→ 混音 ─ preamp ─┐
               └→ convolver(IR 左通道) ─ 湿路高通 130Hz ────────┘  → 输出
  右声道 ─ EQ ─┬─────────────────────────────→ 混音 ─ preamp ─┐
               └→ convolver(IR 右通道) ─ 湿路高通 130Hz ────────┘
```

几个关键设计（详见 [docs/GUIDE.md](docs/GUIDE.md)）：

* **`node.target` 是命门**：不写目标，WirePlumber 会把各预设的输出互相串起来 → 音效层层叠加。
* **preamp 不手写**：离线上算整条链的真实频响峰值，取倒数（等价 EqualizerAPO 的 Preamp）。
* **σmax ≤ 1**：按图拓扑求 2×2 传递矩阵的最大奇异值，代表"任意输入下的最坏增益"。
  混响型另用**解析上界**（噪声型 IR 的频谱起伏太快，网格上取最大值会低估真实峰值）。
* **湿路每声道各送**：既保住混响的立体声像，又避免"相关输入下湿路同相叠加"白吃削顶余量。

## 验证

这套东西的规矩是：**说"没问题"之前先量一遍**。

```
配置加载     21/21 sink 建成，PipeWire 日志零 error/warning
峰值增益     21/21 σmax ≤ 1（数学上保证不削顶），eval_graph.py 一条命令复核
EQ/矩阵实测   null sink 探针 + parec 录回 DSP 输出，与落盘复算对比：
              26 个测点平均偏差 0.062 dB，最大 0.518 dB
混响实测      e2e_reverb.py（白噪声探针法）：
              40 个测点（4 预设 × 5 频带 × 左右）平均偏差 0.225 dB，最大 0.561 dB
              Schroeder 反向积分测 RT60：1.34 / 3.13 / 0.61 / 3.28 s，与设计同量级
             独立手算复核（60 个频点直接 DFT 卷积核）：含 preamp 后 σmax 0.74~0.82
CPU          空闲 0.0%；2 个 4 秒 IR 卷积器同时工作 0.83%（单核百分比）
```

自己复核：

```bash
python3 ~/.local/share/yinxiao/eval_graph.py      # σmax 表格（超限会返回非零退出码）
python3 ~/.local/share/yinxiao/e2e_reverb.py      # 混响端到端实测（静音，用探针录）
```

## 已知限制

* **低音增强是 EQ + 并联低通**，不是谐波合成 —— 需要的 `bass_enhancer` / `loudness` /
  `limiter` 这些内置插件本机没有，要谐波合成请配 EasyEffects / LSP 插件。
* **卷积混响不需要 EasyEffects**：`convolver` 是 PipeWire 自带的内置插件。查一下最放心：
  `strings /usr/lib/spa-0.2/filter-graph/libspa-filter-graph-plugin-builtin.so | grep convolver`。
  若你的构建里没有它，4 个厅堂预设会加载失败（其余 17 个不受影响）。
* **混响时间不能实时调**（RT60 是 IR 的属性），要改就改 `gen_irs.py` 里的厅堂参数重生成；
  干湿比可以实时调。
* preamp 归一化后各预设整体音量不同（低频抬得多的更轻），这是防削顶的必然代价。
* 换声卡 / 换机器后要重跑 `install.sh`（`node.target` 写的是设备名）。
* `install.sh` 会按仓库参数**重新生成** conf —— 编辑器里改过的参数请先备份
  （编辑器每次保存都会留 `.bak`）。

## 目录结构

```
bin/yinxiao              命令行
lib/dsp.py               DSP 内核（唯一的数学实现：求值 / 分析 / 读写 conf / 运行时对接）
lib/gen_presets.py       17 个 EQ / 矩阵型预设生成器
lib/gen_irs.py           厅堂脉冲响应合成器
lib/gen_reverb.py        4 个混响预设生成器
lib/eval_graph.py        σmax 验证器
lib/e2e.py               EQ / 矩阵型端到端实测
lib/e2e_reverb.py        混响端到端实测
lib/editor_server.py    编辑器后端（仅监听 127.0.0.1:8787）
lib/editor.html         编辑器前端（构建产物：单文件，Tailwind + Preact/htm 内联）
editor/                 前端源码与构建链（Tailwind 入口、Preact/htm vendor、build.sh）
systemd/                掉线自愈 + 编辑器常驻两个 user 服务
docs/GUIDE.md           完整教程：原理、日常使用、踩过的坑、故障排查
```

改前端：编辑 `editor/editor.src.html`，然后 `bash editor/build.sh`（需要 node，**只在开发期**；
用户安装不需要）。构建是幂等的：连续两次构建产物逐字节一致。

## 许可

本项目 MIT。前端产物内联了两个第三方库，均为 MIT，源文件在 `editor/vendor/`：

* [Tailwind CSS](https://tailwindcss.com)（MIT）—— 只保留源码里用到的类，编译后内联进单文件
* [Preact](https://preactjs.com) 10 与 [htm](https://github.com/developit/htm)（MIT）—— UMD 构建内联，
  组件用 htm 模板字符串写，无构建期转译

许可证原文见 `editor/vendor/licenses/`。Python 侧不依赖任何第三方包。
