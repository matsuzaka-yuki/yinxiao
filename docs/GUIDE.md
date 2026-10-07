# yinxiao · PipeWire 音效系统 完全教程

> 这套东西解决的问题：**Windows 下的杜比 / DTS / Realtek 音效是闭源 OEM 驱动，无法移植到 Linux**，
> 所以用 PipeWire 的 filter-chain 重建一套等价的 DSP 能力。
> 本机实测环境：CachyOS / 内核 7.x / PipeWire 1.6.9 + WirePlumber 0.5.18 / Python 3.14
> （其它发行版只要 PipeWire 的 builtin 插件齐全就能跑，见下面「已知限制」）。

---

## 1. 它长什么样

```
  应用（Chrome / QQ音乐 / 播放器）
        │  把默认输出设成某个预设
        ▼
  ┌─────────────────────────────┐
  │ effect_input.yx_超重低音      │  ← 虚拟 sink（应用看到的就是它）
  │   ↓  一串双二阶滤波器          │
  │   ↓  预增益 preamp            │
  │ effect_output.yx_超重低音     │  ← 用 node.target 钉死在物理声卡上
  └─────────────────────────────┘
        ▼
   物理声卡（USB DAC）
```

混响型（厅堂）预设多两步卷积：

```
  左声道 ─ EQ ─┬─────────────────────────────→ 混合 ─ preamp ─┐
               └→ convolver(ch0，立体声 IR 的左通道) ─ 湿路高通 ─┘   → 输出
  右声道 ─ EQ ─┬─────────────────────────────→ 混合 ─ preamp ─┐
               └→ convolver(ch1)              ─ 湿路高通 ──────┘
```

**21 个预设的链路是常驻的**，一起加载、互不干扰。切换音效只是"把默认输出换一个 sink"，
不重启任何服务、不断音。

| 类别 | 预设 |
|---|---|
| 低音 | 超重低音 / 完美低音 / 轻低音 / 重低音炮 |
| 人声 | 清澈人声 / 磁性人声 / 贴耳人声 |
| 曲风 | 流行 / 摇滚 / 电子 / 古典 / 爵士 / 暖声黑胶 |
| 空间 | 3D 环绕 / HIFI 现场 / 空气感 / 等响补偿 |
| 厅堂（卷积混响） | 演唱会 / 音乐厅 / Live House / 大教堂 |

---

## 2. 文件都在哪

| 路径 | 作用 | 备份必要性 |
|---|---|---|
| `~/.config/pipewire/pipewire.conf.d/50-yinxiao-*.conf` | **21 个预设本体**（滤波器系数、链路） | ✅ 必须 |
| `~/.local/bin/yinxiao` | 命令行控制（切换 / 状态 / 修复 / 守护） | ✅ 必须 |
| `~/.local/share/yinxiao/dsp.py` | DSP 核心：图解析、频响分析、写盘、链路检查 | ✅ 必须 |
| `~/.local/share/yinxiao/editor_server.py` | 网页编辑器后端（HTTP + 实时改参数） | ✅ 必须 |
| `~/.local/share/yinxiao/editor.html` | 网页编辑器前端（单文件，无依赖） | ✅ 必须 |
| `~/.local/share/yinxiao/gen_presets.py` | EQ / 矩阵型预设生成器（前 17 个） | ✅ 必须 |
| `~/.local/share/yinxiao/gen_irs.py` | 厅堂 IR 合成器（后 4 个预设的混响核） | ✅ 必须 |
| `~/.local/share/yinxiao/gen_reverb.py` | 混响预设生成器 | ✅ 必须 |
| `~/.local/share/yinxiao/ir/*.wav` | 4 个立体声 IR（24bit）+ `.resp.json` 频响缓存 | ✅ 必须 |
| `~/.local/share/yinxiao/eval_graph.py` | 峰值增益验证器（σmax ≤ 1） | ✅ 必须 |
| `~/.local/share/yinxiao/e2e.py` / `e2e_reverb.py` | 端到端实测脚本（静音，用探针录） | ⬜ 建议 |
| `~/.local/share/yinxiao/presets.tsv` | 预设参数表（id / 中文名 / preamp / 说明） | ✅ 必须 |
| `~/.local/share/yinxiao/hw-sink` | **物理声卡的 sink 名**（唯一的机器相关配置） | ✅ 必须 |
| `~/.local/share/yinxiao/last-target` | 上次选择的输出，供自愈恢复 | ⬜ 自动生成 |
| `~/.config/systemd/user/yinxiao-heal.service` | 自愈守护（DAC 掉线再上线自动恢复） | ✅ 必须 |
| `~/.local/share/yinxiao/README.md` | 简版说明 | ⬜ 建议 |

**开机自启**：17 个预设由 PipeWire 自己在登录时加载
（`pipewire.socket` / `pipewire-pulse.socket` / `wireplumber.service` 都是系统自带的 user 单元）。
网页编辑器由 `yinxiao-editor.service` 常驻（已 enable + linger，开机即起，监听 8787），
它不参与音频链路。

---

## 3. 日常使用（命令行）

```bash
yinxiao list          # 列出 17 个预设，● 标记当前正在输出的
yinxiao 超重低音       # 切换（也可以写 bass-xxl，支持前缀/包含匹配）
yinxiao off           # 关闭音效，直通
yinxiao status        # 看状态；有问题会告警
yinxiao next / prev   # 上/下一个预设
yinxiao edit          # 启动网页编辑器（127.0.0.1:8787）
yinxiao edit-stop     # 关掉网页编辑器
yinxiao fix           # 修复被搬歪的链路（幂等，可反复跑）
yinxiao heal          # 守护进程，一般由 systemd 跑，不用手动
```

---

## 4. 网页编辑器

`yinxiao edit` 后浏览器打开 `http://127.0.0.1:8787`。

- **点一下左侧预设 = 立即切换输出**（和 QQ音乐一样）。不想这样就把顶栏「点击即切换输出」取消勾选。
- 预设名后面的**绿点 ●** = 此刻正在输出的那个。
- 改过但没保存的会标 `*`。
- 曲线区：**拖圆点**改频率/增益、**滚轮**改 Q、**Del** 删除选中的点。
- 「自动归一化（防削顶）」勾上时，preamp 会随你的改动自动重算，保证不会削顶。
- 「保存到磁盘」才真正写回 conf（会留一份 `.bak`）；「回滚上次保存」从 `.bak` 还原。
  内容没有实质变化时不会写盘、也不动 `.bak`。
- **混响预设**（演唱会 / 音乐厅 / Live House / 大教堂）左栏点开会换成**混响面板**：
  干湿比实时可调，右下角同步显示「1kHz 湿/干」；RT60 / 预延迟 / 湿路高通 / IR 文件名是只读信息。
  曲线画的是 1/3 倍频程平滑后的响应 —— 浅色细线才是 IR 的真实频谱，
  它是噪声型的、本来就是一排尖刺，平滑曲线才是听感走向。

---

## 5. 踩过的坑（这一节最重要，重装前必读）

### 5.1 `node.target` 是整套东西的命门
每个 `effect_output` 都带 `node.target = <物理声卡>`。
**没有它**，WirePlumber 会把 17 个虚拟输出的自动路由接到彼此的输入上（星形串接），
信号层层叠加、音频跑到错误设备。

### 5.2 绝对不要 `move-sink-input` 自己的预设输出流
```bash
# ❌ 致命：把 effect_output.* 也一起搬走，破坏 node.target 钉定
for si in $(pactl list short sink-inputs | awk '{print $1}'); do
    pactl move-sink-input "$si" "$target"; done
# ✅ 只搬"应用"的流
```
这就是本项目历史上真实出过的事故：一次点击把 17 条链路全搬走，
WirePlumber 随后把当前预设的输出接到了一个**完全不同的设备**（S/PDIF）上，整机没声音。
`yinxiao` 与 `editor_server.py` 里都有 `app_streams()` / `dsp.app_sink_inputs()` 专门做这个过滤。

### 5.3 WirePlumber 选默认设备的优先级
- 候选设备按 `priority.session` 排名（USB DAC 通常 ~1109，虚拟 sink 无此属性 = 0）。
- **但 `~/.local/state/wireplumber/default-nodes` 里记录的默认设备优先级是 20001**，压过一切。
- 所以曾经出现过"把默认设成某个预设后，每次重启 PipeWire 都被恢复成那个预设"。
  排查命令：`grep audio.sink= ~/.local/state/wireplumber/default-nodes`。

### 5.4 `pw-link` 的 link / unlink 是异步的
断连之后**立刻**重连会失败（实测必现）。所有重连都必须重试 + 校验：
见 `yinxiao` 里的 `link_to_target()`。

### 5.5 解析 `pw-link -l` 的陷阱
`pw-link -l` **每条连线会列两次**（源端口下一行 `|->`，目标端口下一行 `|<-`）。
只取 `|->` 视角，否则同一连线会被数成两条，看起来像"重复连接"。
要拿地面真值就用 `pw-dump` 的 `PipeWire:Interface:Link` 对象。

### 5.6 preamp 必须由整条链的真实频响算出
手写一个固定 preamp 会让中频被压掉约 4dB。
正确做法：用 RBJ 公式离线算出整条链的峰值增益，取倒数当 preamp。
`dsp.py` 的 `analyze()` 就是干这个的，`σmax` 是"峰值增益"指标。

### 5.7 USB DAC 开机枚举失败
某些 USB DAC（尤其插机箱前面板时）偶发，内核日志长这样：
```
usb 3-7: device descriptor read/64, error -71
usb 3-7: device not accepting address 7, error -71
```
`-71` = EPROTO（设备不响应）。这时 DAC 根本没进内核 → 没有 ALSA 卡 → 没有 PipeWire sink →
**声音会静默落到别的输出设备上，听起来就是"突然没声音了"**。
插拔一次即可恢复。`yinxiao-heal.service` 就是为这个准备的。

### 5.8 IR 的归一化增益必须真的写进 WAV
混响的绝对电平是按「IR 的频谱峰值 = 0.85」定的。第一版把归一化只写进了频响缓存
（`*.resp.json`，供 σmax 校验与编辑器画曲线用），**WAV 文件本身没跟着缩放** ——
于是模型算出湿路增益 0.50、实际 convolver 拿到 5.27，差 20dB（会直接把耳朵和音箱送走）。
是"用独立方法手算一遍传递函数"逮到的：模型与手算对不上，就一定是模型或数据有一方错了。
**结论**：任何"元数据"式的归一化都必须落到被真正读取的文件本身上。

### 5.9 噪声型信号不能用网格上的 σmax 判定
混响 IR 是噪声型信号，单个 FFT bin 的幅值起伏有 ±5dB（1.4s 的窗在低频只有 ~2 个自由度）。
1/48 倍频程网格上的最大值比真实峰值低 0~3.3dB（实测），
拿它算 preamp 会偷偷削顶。所以混响的判定改用**解析上界**：

```
σmax ≤ max|EQ(f)| · (dry + x·wet·max|H_湿路高通(f)|·IR 峰值)
```

三项里前两项是双二阶（平滑函数，网格误差 <0.05dB）、第三项是写 WAV 时归一的常数，
所以这个上界不依赖网格。`x` 是湿路串扰系数：每声道各自送混响 → x=1；
若"左右汇总成一路再送两声道"，对 L=R 的相关输入会同相叠加 → x=2（和 sub-woofer 同理）。
本项目用前者，顺便也保住了混响的立体声像。

### 5.10 builtin `delay` 做不了干净的预延迟
`delay` 节点的 Feedback/Feedforward 控制**被钳在 ±10dB**，"无反馈"取不到干净值，
拿它做预延迟会引入梳状染色。所以预延迟直接**烘进 IR**（IR 开头一段静音），
零代价、可验证。要改就改 `gen_irs.py` 里的 `pre_ms` 重生成 IR。

### 5.11 DSP 数学不要抄第二份
`eval_graph.py` 原本是一份独立复制的求值器。加了 convolver 之后它不认识新节点、
把卷积当成单位增益，于是把 4 个正常的混响预设全部误报成"✘ 超限"。
现在它只调 `dsp.py` 的求值器（数学一份、口径一致），教训：**抄一份数学 = 埋一颗地雷**。

---

## 6. 没声音了怎么查（按顺序）

```bash
# 1) 物理设备还在吗
lsusb | grep -i icon
cat /proc/asound/cards
pactl list short sinks | grep -i usb        # 或按你的声卡关键字过滤

# 2) 看得懂中文的结论
yinxiao status

# 3) 链路有没有被搬歪
yinxiao fix

# 4) 内核有没有报 USB 错误
journalctl -b | grep -i 'usb.*error'
```

`yinxiao status` 会直接告诉你三件事：当前音效、默认输出、以及
「物理输出设备不在系统里」/「有 N 个预设输出链路异常」这两类告警。

---

## 7. 改预设 / 加预设

参数表在 `presets.tsv`，生成器是 `gen_presets.py`，DSP 核心是 `dsp.py`。

```bash
# ① EQ / 矩阵型（前 17 个，会按当前 hw-sink 钉定）
python3 ~/.local/share/yinxiao/gen_presets.py ~/.config/pipewire/pipewire.conf.d/ "$(cat ~/.local/share/yinxiao/hw-sink)"

# ② 厅堂混响（后 4 个）：厅堂参数在 gen_irs.py 的 HALLS 里
#    （rt60 / pre_ms / wet_db=目标干湿比 / tone=干路音色）
python3 ~/.local/share/yinxiao/gen_irs.py            # 合成 IR（几秒）
python3 ~/.local/share/yinxiao/gen_reverb.py --dry-run   # 先看 σmax 与湿干比，不写系统
python3 ~/.local/share/yinxiao/gen_reverb.py         # 写入 conf.d + 更新 presets.tsv
systemctl --user restart pipewire pipewire-pulse
```

混响生成器会自动**反解干湿比**：你给的是"1kHz 处湿路相对干路多少 dB"（听感口径），
它算出对应的 mixer 增益，并把 preamp 按解析上界归一化。

滤波器类型（RBJ 双二阶）：`HP` / `LP` / `PK`(peaking) / `LS`(lowshelf) / `HS`(highshelf)，
声场类用 M/S 矩阵加宽。

**校验规则**：每个预设的最坏增益上界必须 ≤ 1.0，代表"任意输入下都不会削顶"
（混响型以解析上界为准）。改动后一定要复核：

```bash
python3 ~/.local/share/yinxiao/eval_graph.py      # 打表格：σmax / 余量 / 判定
python3 ~/.local/share/yinxiao/e2e_reverb.py      # 混响型再加一步端到端实测（静音）
```

---

## 8. 换设备 / 迁移到别的机器

换声卡后，`node.target` 会失效（它写死了设备名）。重新跑一遍安装即可：

```bash
bash install.sh --sink <新的 sink 名>      # 会重写 hw-sink 并重新生成全部 conf
```

迁移到新机器：把仓库 clone 下来，跑一次 `install.sh` ——
它会自己检测声卡、生成 conf 与 IR、装好命令行与两个 systemd 服务。

---

## 9. 已知限制

- 当前 PipeWire 构建**缺少**这些内置插件：`bass_enhancer`（谐波合成型低音增强）、
  `loudness`、`limiter`、`virtual-surround-sink`。
  所以"重低音"是纯 EQ 低通，不是谐波合成。
- 想要谐波合成 / 多段压缩，用 EasyEffects 补：
  `sudo pacman -S easyeffects lsp-plugins calf zam-plugins`
  （EasyEffects 有自己独立的预设体系，改不了本项目的预设，两者共存即可。）
- **卷积混响不需要 EasyEffects**：本机 builtin 插件里就有 `convolver`
  （还有 `delay` / `noisegate` / `dcblock` 等），4 个厅堂预设就是用它做的。
  查一遍最放心：`strings /usr/lib/spa-0.2/filter-graph/libspa-filter-graph-plugin-builtin.so | grep convolver`。
- 混响的 RT60 是 IR 的属性，**不能实时调**；干湿比可以（编辑器面板 / 实时试听）。
- 21 条链路常驻会多占一点内存和 CPU。实测：空闲 0.0%，
  2 个 4s IR 卷积器同时工作 0.83%（单核百分比），可以忽略。

---

## 10. 一键自检

```bash
cat <<'EOF' | bash
echo "预设文件: $(ls ~/.config/pipewire/pipewire.conf.d/50-yinxiao-*.conf 2>/dev/null | wc -l) / 21"
echo "自愈服务: $(systemctl --user is-active yinxiao-heal 2>/dev/null)"
echo "编辑器  : $(systemctl --user is-active yinxiao-editor 2>/dev/null) (127.0.0.1:8787)"
echo "物理声卡: $(pactl list short sinks | grep -c usb) (1=在)"
echo "混响 IR : $(ls ~/.local/share/yinxiao/ir/*.wav 2>/dev/null | wc -l) / 4"
grep -c convolver ~/.config/pipewire/pipewire.conf.d/50-yinxiao-*.conf | tail -4
yinxiao status
EOF
```
