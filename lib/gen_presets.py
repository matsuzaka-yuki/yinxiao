#!/usr/bin/env python3
"""生成 PipeWire filter-chain 音效预设。
关键设计：preamp 不手写，而是用 RBJ 公式离线算出整条链的频响峰值，
自动归一化到 0 dB（等价于 EqualizerAPO 的 Preamp），保证永不削顶。"""
import json, os, sys, math, cmath

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/pwprobe/cfg2/pipewire/pipewire.conf.d"
FS  = 48000.0
HWFILE = os.path.expanduser("~/.local/share/yinxiao/hw-sink")

def hw_sink():
    """物理声卡 sink 名：命令行第二参数优先，其次读 hw-sink。

    它会被写进每个 conf 的 playback.props.node.target —— 少了这一行，
    WirePlumber 会把各预设的输出互相串接（音效层层叠加 / 跑到别的设备）。
    """
    if len(sys.argv) > 2 and sys.argv[2].strip():
        return sys.argv[2].strip()
    try:
        return open(HWFILE, encoding="utf-8").read().strip()
    except OSError:
        return ""

HW = hw_sink()

# ---------------- RBJ 双二阶（与 PipeWire bq_* 实现一致）----------------
def biquad(label, f0, Q, gain_db):
    A  = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / FS
    cw, sw = math.cos(w0), math.sin(w0)
    if label == "bq_peaking":
        al = sw / (2 * Q)
        b = [1 + al * A, -2 * cw, 1 - al * A]; a = [1 + al / A, -2 * cw, 1 - al / A]
    elif label in ("bq_lowshelf", "bq_highshelf"):
        al = sw / 2 * math.sqrt((A + 1 / A) * (1 / Q - 1) + 2)
        sq = 2 * math.sqrt(A) * al
        if label == "bq_lowshelf":
            b = [A*((A+1)-(A-1)*cw+sq), 2*A*((A-1)-(A+1)*cw), A*((A+1)-(A-1)*cw-sq)]
            a = [(A+1)+(A-1)*cw+sq, -2*((A-1)+(A+1)*cw), (A+1)+(A-1)*cw-sq]
        else:
            b = [A*((A+1)+(A-1)*cw+sq), -2*A*((A-1)+(A+1)*cw), A*((A+1)+(A-1)*cw-sq)]
            a = [(A+1)-(A-1)*cw+sq, 2*((A-1)-(A+1)*cw), (A+1)-(A-1)*cw-sq]
    elif label == "bq_highpass":
        al = sw / (2 * Q); b = [(1+cw)/2, -(1+cw), (1+cw)/2]; a = [1+al, -2*cw, 1-al]
    elif label == "bq_lowpass":
        al = sw / (2 * Q); b = [(1-cw)/2, 1-cw, (1-cw)/2];    a = [1+al, -2*cw, 1-al]
    else:
        raise ValueError(label)
    return [x/a[0] for x in b], [x/a[0] for x in a]

def curve_peak(stages):
    """返回 (峰值dB, 峰值频率, {频率: dB})"""
    z_cache = {}
    best, bestf = -1e9, 0
    pts = {}
    for i in range(240):                      # 20Hz~20kHz，1/24 倍频程
        f = 20.0 * (1000.0 ** (i / 239.0))
        z = cmath.exp(-2j * math.pi * f / FS)
        tot = 1 + 0j
        for (lb, f0, Q, g) in stages:
            b, a = biquad(lb, f0, Q, g)
            tot *= (b[0] + b[1]*z + b[2]*z*z) / (a[0] + a[1]*z + a[2]*z*z)
        db = 20 * math.log10(abs(tot))
        pts[f] = db
        if db > best: best, bestf = db, f
    return best, bestf, pts

# ---------------- 构件 ----------------
def bq(name, label, freq=None, q=None, gain=None):
    c = {}
    if freq is not None: c["Freq"] = float(freq)
    if q    is not None: c["Q"]    = float(q)
    if gain is not None: c["Gain"] = float(gain)
    return {"type": "builtin", "name": name, "label": label, "control": c}
HP = lambda n, f, q=0.7: bq(n, "bq_highpass",  f, q)
LP = lambda n, f, q=0.7: bq(n, "bq_lowpass",   f, q)
LS = lambda n, f, g, q=0.7: bq(n, "bq_lowshelf",  f, q, g)
HS = lambda n, f, g, q=0.7: bq(n, "bq_highshelf", f, q, g)
PK = lambda n, f, g, q=1.0: bq(n, "bq_peaking",   f, q, g)

def eq_chain(bands):
    nodes = [n for _, n in bands]
    links = [{"output": f"{bands[i][0]}:Out", "input": f"{bands[i+1][0]}:In"}
             for i in range(len(bands) - 1)]
    return nodes, links

def stages_of(nodes):
    out = []
    for n in nodes:
        cl, c = n["label"], n["control"]
        if cl in ("bq_peaking", "bq_lowshelf", "bq_highshelf"):
            out.append((cl, c["Freq"], c["Q"], c["Gain"]))
        elif cl in ("bq_highpass", "bq_lowpass"):
            out.append((cl, c["Freq"], c["Q"], 0.0))
    return out

# 宽带衰减：20kHz lowshelf 负增益 ≈ 整段等量下拉（preamp 用）
def trim_node(db):
    # 用 20Hz 的 highshelf 做宽带衰减：40Hz 以上即进入平台区，衰减量精确等于 db，
    # 误差仅落在 <40Hz 的次声区（已被各预设的 highpass 清掉）。
    return HS("pre", 5.0, db)

# ---------------- 预设表（只写"音色塑形"，preamp 自动算）----------------
EQ_PRESETS = [
    ("bass-xxl", "超重低音", "低频猛推 + 去中低浑浊，鼓点贝斯砸出来", [
        ("hp", HP("hp", 22)), ("b1", LS("b1", 55, 6.5)), ("p1", PK("p1", 90, 3.5, 0.9)),
        ("p2", PK("p2", 400, -2.0, 1.0)), ("hs", HS("hs", 9000, -1.5)),
    ]),
    ("bass-perfect", "完美低音", "比超重低音克制，下潜深但不糊", [
        ("hp", HP("hp", 25)), ("b1", LS("b1", 80, 5.5)), ("p1", PK("p1", 60, 2.5, 1.0)),
        ("p2", PK("p2", 300, -1.5, 1.0)), ("hs", HS("hs", 10000, -0.5)),
    ]),
    ("bass-soft", "轻低音", "小幅低频提振，适合长时间听", [
        ("hp", HP("hp", 28)), ("b1", LS("b1", 100, 3.0)), ("p1", PK("p1", 350, -1.0, 1.0)),
    ]),
    ("vocal-clear", "清澈人声", "切低频泥、抬 1.8-3.2kHz，人声前凸", [
        ("hp", HP("hp", 75)), ("p1", PK("p1", 220, -2.5, 1.0)), ("p2", PK("p2", 1800, 3.0, 1.2)),
        ("p3", PK("p3", 3200, 2.5, 1.5)), ("hs", HS("hs", 9500, 2.0)),
    ]),
    ("vocal-magnetic", "磁性人声", "深夜电台感，厚而不闷", [
        ("hp", HP("hp", 90)), ("b1", LS("b1", 200, 2.5)), ("p1", PK("p1", 400, -1.0, 1.0)),
        ("p2", PK("p2", 1200, 1.5, 1.2)), ("hs", HS("hs", 6500, -3.5)),
    ]),
    ("pop", "流行", "低频微抬 + 高频亮，通用好听", [
        ("hp", HP("hp", 30)), ("b1", LS("b1", 110, 2.5)), ("p1", PK("p1", 350, -1.0, 1.0)),
        ("p2", PK("p2", 2500, 2.0, 1.2)), ("hs", HS("hs", 8000, 2.0)),
    ]),
    ("rock", "摇滚", "低频冲击 + 3kHz 咬合感 + 空气感", [
        ("hp", HP("hp", 30)), ("b1", LS("b1", 90, 3.0)), ("p1", PK("p1", 500, -2.0, 1.0)),
        ("p2", PK("p2", 3000, 3.0, 1.4)), ("p3", PK("p3", 5500, 1.5, 2.0)),
        ("hs", HS("hs", 9000, 1.5)),
    ]),
    ("electronic", "电子", "重低音 + 高频刺激，蹦迪口径", [
        ("hp", HP("hp", 25)), ("b1", LS("b1", 70, 5.0)), ("p1", PK("p1", 45, 2.5, 1.0)),
        ("p2", PK("p2", 900, -1.5, 1.0)), ("p3", PK("p3", 4000, 2.0, 1.5)),
        ("hs", HS("hs", 10000, 2.5)),
    ]),
    ("classical", "古典", "接近平直，只补一点厅堂空气感", [
        ("hp", HP("hp", 28)), ("b1", LS("b1", 100, 1.5)), ("p1", PK("p1", 400, -0.8, 1.0)),
        ("p2", PK("p2", 1200, 0.8, 1.5)), ("hs", HS("hs", 12000, 1.5)),
    ]),
    ("jazz", "爵士", "中低暖、高频柔，铜管不刺耳", [
        ("hp", HP("hp", 30)), ("b1", LS("b1", 140, 2.5)), ("p1", PK("p1", 300, 1.2, 1.0)),
        ("p2", PK("p2", 2000, -1.0, 1.2)), ("hs", HS("hs", 8000, -1.5)),
    ]),
    ("warm-vinyl", "暖声黑胶", "削高频数码味 + 中低加厚，听久不累", [
        ("hp", HP("hp", 40)), ("b1", LS("b1", 150, 2.5)), ("p1", PK("p1", 500, 1.5, 0.9)),
        ("p2", PK("p2", 2500, -1.2, 1.2)), ("hs", HS("hs", 7000, -3.5)),
    ]),
    ("air", "空气感", "削 300Hz 浑浊、抬极高频，通透开扬", [
        ("hp", HP("hp", 35)), ("p1", PK("p1", 300, -1.5, 1.0)), ("p2", PK("p2", 6000, 2.0, 1.5)),
        ("hs", HS("hs", 12000, 3.5)),
    ]),
    ("loudness", "等响补偿", "小音量下仍听得见低音和细节", [
        ("hp", HP("hp", 30)), ("b1", LS("b1", 120, 5.0)), ("p1", PK("p1", 2500, 1.5, 1.2)),
        ("hs", HS("hs", 9000, 1.5)),
    ]),
]

# ---------------- 矩阵型（跨声道）----------------
def widen_nodes(k, bass):
    # 对称矩阵 [[1+k,-k],[-k,1+k]] 的特征值为 (1+2k) 与 1，
    # 故 σmax = peaklin * max(1, |1+2k|)；取其倒数作 preamp，精确归一。
    peak_lin = 10 ** (curve_peak([("bq_lowshelf", 120.0, 0.7, bass)])[0] / 20) if bass else 1.0
    g = 1.0 / (peak_lin * max(1.0, abs(1 + 2 * k)))
    return ([bq("iL", "copy"), bq("iR", "copy"), LS("eL", 120, bass), LS("eR", 120, bass),
             {"type": "builtin", "name": "wL", "label": "mixer",
              "control": {"Gain 1": round(g * (1 + k), 6), "Gain 2": round(g * -k, 6)}},
             {"type": "builtin", "name": "wR", "label": "mixer",
              "control": {"Gain 1": round(g * -k, 6), "Gain 2": round(g * (1 + k), 6)}},
             bq("oL", "copy"), bq("oR", "copy")],
            [{"output": "iL:Out", "input": "eL:In"},
             {"output": "iR:Out", "input": "eR:In"},
             {"output": "eL:Out", "input": "wL:In 1"},
             {"output": "eL:Out", "input": "wR:In 1"},
             {"output": "eR:Out", "input": "wL:In 2"},
             {"output": "eR:Out", "input": "wR:In 2"},
             {"output": "wL:Out", "input": "oL:In"},
             {"output": "wR:Out", "input": "oR:In"}],
            ["iL:In", "iR:In"], ["oL:Out", "oR:Out"])

def sub_nodes(sub_gain, xfreq):
    # 低频相关时最坏增益 = 1 + s（lowpass 峰值增益为 1），折入 preamp 后
    # 表现为"中频降 (1+s) 倍、低频顶到满量程"，等效 +20log10(1+s) dB 低音提振。
    g = 1.0 / (1.0 + sub_gain)
    return ([bq("iL", "copy"), bq("iR", "copy"),
             {"type": "builtin", "name": "mix", "label": "mixer",
              "control": {"Gain 1": 0.5, "Gain 2": 0.5}},
             LP("lp", xfreq),
             {"type": "builtin", "name": "fL", "label": "mixer",
              "control": {"Gain 1": round(g, 6), "Gain 2": round(g * sub_gain, 6)}},
             {"type": "builtin", "name": "fR", "label": "mixer",
              "control": {"Gain 1": round(g, 6), "Gain 2": round(g * sub_gain, 6)}},
             bq("oL", "copy"), bq("oR", "copy")],
            [{"output": "iL:Out", "input": "mix:In 1"},
             {"output": "iR:Out", "input": "mix:In 2"},
             {"output": "mix:Out", "input": "lp:In"},
             {"output": "iL:Out", "input": "fL:In 1"},
             {"output": "lp:Out", "input": "fL:In 2"},
             {"output": "iR:Out", "input": "fR:In 1"},
             {"output": "lp:Out", "input": "fR:In 2"},
             {"output": "fL:Out", "input": "oL:In"},
             {"output": "fR:Out", "input": "oR:In"}],
            ["iL:In", "iR:In"], ["oL:Out", "oR:Out"])

MATRIX_PRESETS = [
    ("surround-3d", "3D 环绕", "M/S 矩阵加宽声场，耳机现场感（单声道兼容）",
     widen_nodes(0.40, 0.0)),
    ("wide-live", "HIFI 现场", "中等加宽 + 低频托底，像站在舞台中前排",
     widen_nodes(0.21, 0.0)),
    ("mono-intimate", "贴耳人声", "负宽度收窄声场，人声贴脸，适合睡前",
     widen_nodes(-0.30, 0.0)),
    ("sub-woofer", "重低音炮", "并联低通 sub 通道，普通音箱也能挤出低频",
     sub_nodes(0.65, 90)),
]

# ---------------- 输出 ----------------
def conf(preset_id, desc, nodes, links, inputs=None, outputs=None):
    graph = {"nodes": nodes, "links": links}
    nid = preset_id.replace("-", "_")
    chain = {
        "node.description": f"音效 · {desc}",
        "media.name":       f"音效 · {desc}",
        "filter.graph": graph,
        "capture.props":  {"node.name": f"effect_input.yx_{nid}", "media.class": "Audio/Sink",
                           "node.description": f"音效 · {desc}",
                           "audio.channels": 2, "audio.position": ["FL", "FR"]},
        "playback.props": {"node.name": f"effect_output.yx_{nid}", "node.passive": True,
                           "audio.channels": 2, "audio.position": ["FL", "FR"]},
    }
    if HW:
        chain["playback.props"]["node.target"] = HW
    if inputs or outputs:
        graph["inputs"], graph["outputs"] = inputs, outputs
    else:
        chain["audio.channels"], chain["audio.position"] = 2, ["FL", "FR"]
    return {"context.modules": [{"name": "libpipewire-module-filter-chain", "args": chain}]}

os.makedirs(OUT, exist_ok=True)
manifest, idx = [], 0

for pid, name, note, bands in EQ_PRESETS:
    nodes, links = eq_chain(bands)
    peak_db, peak_f, pts = curve_peak(stages_of(nodes))     # ← 实测峰值
    preamp = -math.ceil((peak_db + 0.25) * 4) / 4   # 0.25dB 安全余量，向上取整                        # 0.5dB 步进
    nodes.append(trim_node(preamp))
    links.append({"output": nodes[-2]["name"] + ":Out", "input": "pre:In"})
    idx += 1
    with open(f"{OUT}/50-yinxiao-{idx:02d}-{pid}.conf", "w") as f:
        f.write(f"# yinxiao preset: {pid}  preamp={preamp:+.1f}dB (auto, peak {peak_db:+.1f}dB @{peak_f:.0f}Hz)\n")
        f.write(json.dumps(conf(pid, name, nodes, links), ensure_ascii=False, indent=2) + "\n")
    manifest.append((pid, name, note, preamp, peak_db, peak_f))

for pid, name, note, (nodes, links, inputs, outputs) in MATRIX_PRESETS:
    idx += 1
    with open(f"{OUT}/50-yinxiao-{idx:02d}-{pid}.conf", "w") as f:
        f.write(f"# yinxiao preset: {pid}\n")
        f.write(json.dumps(conf(pid, name, nodes, links, inputs, outputs),
                           ensure_ascii=False, indent=2) + "\n")
    manifest.append((pid, name, note, None, None, None))

with open(os.path.join(OUT, "..", "presets.tsv"), "w") as f:
    f.write("#id\tdisplay\tpreamp\tdesc\n")
    for pid, name, note, preamp, pk, pkf in manifest:
        f.write(f"{pid}\t{name}\t{preamp if preamp is not None else '-'}\t{note}\n")

if HW:
    print(f"物理声卡（写进 node.target）: {HW}")
else:
    print("⚠ 未指定物理声卡：conf 里不会有 node.target ——")
    print("  WirePlumber 会把各预设输出互相串接，务必传第二参数或写好 hw-sink 后重跑。")
print(f"生成 {len(manifest)} 个预设 -> {OUT}")
print()
print(f"{'预设':<10}{'显示名':<12}{'自动preamp':>11}{'归一化前峰值':>14}{'峰值频率':>10}")
print("-" * 60)
for pid, name, note, preamp, pk, pkf in manifest:
    if preamp is None:
        print(f"{pid:<10}{name:<12}{'(矩阵型)':>13}")
    else:
        print(f"{pid:<10}{name:<12}{preamp:>10.1f}dB{pk:>13.1f}dB{pkf:>9.0f}Hz")
