#!/usr/bin/env python3
"""生成 4 个「厅堂/现场」卷积混响预设的 filter-chain 配置。

与 gen_presets.py 的关系：那个负责 17 个 EQ/矩阵型预设（单声道图复制到双声道），
这边负责混响型（显式 2 进 2 出，因为混响需要把左右先叠加成一路再送 convolver）。
两者共用 dsp.py 的求值器 —— σmax / 曲线口径完全一致。

信号流（每个预设一张图）：
    iL ─→ tL1..tLn ─┬──────────────────────────────→ mL:In1 ─┐
                    └┐                                      preL → oL
    iR ─→ tR1..tRn ─┼→ mono(0.5/0.5) → pd(预延迟) ─┬→ revL(ch0) ─→ hwL(湿路高通) → mL:In2 ┘
                    └──────────────────────────────→ mR:In1 ─┐
                                                   └→ revR(ch1) ─→ hwR ─────────→ mR:In2 ┘
                                                                                         mR → preR → oR
    · revL/revR 读同一个立体声 IR 的左右两个通道 → 尾巴在两侧不相关，声场自然变宽
    · wet 增益 = 湿路占比；干路保持 1.0，整体电平交给 preL/preR（preamp）归一化

用法:
    python3 gen_reverb.py                 # 写进 ~/.config/pipewire/pipewire.conf.d/ 并更新 presets.tsv
    python3 gen_reverb.py --dry-run       # 只生成到 /tmp/yxreverb 并报告，不碰系统
    python3 gen_reverb.py --wet 0.5       # 临时换一个干湿比看 σmax / 听感电平
"""
import importlib.util, json, math, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dsp

CONFDIR = os.path.expanduser("~/.config/pipewire/pipewire.conf.d")
TSV = os.path.expanduser("~/.local/share/yinxiao/presets.tsv")
HW = os.path.expanduser("~/.local/share/yinxiao/hw-sink")
IRDIR = dsp.IRDIR
START_IDX = 18          # 17 个老预设占用 01..17

# 湿路高通：低频混响只会糊成一团、还白吃削顶余量，专业混响插件都会切掉
WET_HP = 130.0
WET_HP_Q = 0.7


def bq(name, label, freq=None, q=None, gain=None):
    c = {}
    if freq is not None: c["Freq"] = float(freq)
    if q is not None: c["Q"] = float(q)
    if gain is not None: c["Gain"] = float(gain)
    return {"type": "builtin", "name": name, "label": label, "control": c}


SHELF = {"LS": "bq_lowshelf", "HS": "bq_highshelf", "PK": "bq_peaking",
         "HP": "bq_highpass", "LP": "bq_lowpass"}


def reverb_graph(ir_wav, tone, wet):
    nodes, links = [], []

    def add(n):
        nodes.append(n); return n["name"]

    add({"type": "builtin", "name": "iL", "label": "copy"})
    add({"type": "builtin", "name": "iR", "label": "copy"})
    # 左右各自的音色链（预设性格：低切、中频塑形、空气感）
    for side in ("L", "R"):
        prev = f"i{side}"
        for k, (kind, f0, q, g) in enumerate(tone):
            nm = add(bq(f"t{side}{k + 1}", SHELF[kind], f0, q, g))
            links.append({"output": f"{prev}:Out", "input": f"{nm}:In"})
            prev = nm
    for ch, nm in ((0, "revL"), (1, "revR")):
        add({"type": "builtin", "name": nm, "label": "convolver",
             "config": {"filename": ir_wav, "channel": ch, "blocksize": 128, "gain": 1.0},
             "control": {}})
    for side in ("L", "R"):
        add(bq(f"hw{side}", "bq_highpass", WET_HP, WET_HP_Q))
    for side in ("L", "R"):
        add({"type": "builtin", "name": f"m{side}", "label": "mixer",
             "control": {"Gain 1": 1.0, "Gain 2": round(wet, 6)}})
    for side in ("L", "R"):
        add(bq(f"pre{side}", "bq_highshelf", 5.0, 0.7, 0.0))
    for side in ("L", "R"):
        add({"type": "builtin", "name": f"o{side}", "label": "copy"})

    tailL = f"tL{len(tone)}:Out" if tone else "iL:Out"
    tailR = f"tR{len(tone)}:Out" if tone else "iR:Out"
    links = [l for l in links] + [
        {"output": tailL, "input": "revL:In"},     # 每声道各自送混响：保住立体声像，
        {"output": tailR, "input": "revR:In"},     # 也避免"相关输入下湿路叠加 ×2"吃掉削顶余量
        {"output": "revL:Out", "input": "hwL:In"},
        {"output": "revR:Out", "input": "hwR:In"},
        {"output": tailL, "input": "mL:In 1"},
        {"output": tailR, "input": "mR:In 1"},
        {"output": "hwL:Out", "input": "mL:In 2"},
        {"output": "hwR:Out", "input": "mR:In 2"},
        {"output": "mL:Out", "input": "preL:In"},
        {"output": "mR:Out", "input": "preR:In"},
        {"output": "preL:Out", "input": "oL:In"},
        {"output": "preR:Out", "input": "oR:In"},
    ]
    return {"nodes": nodes, "links": links,
            "inputs": ["iL:In", "iR:In"], "outputs": ["oL:Out", "oR:Out"]}


def conf(preset_id, desc, graph):
    nid = preset_id.replace("-", "_")
    chain = {
        "node.description": f"音效 · {desc}",
        "media.name": f"音效 · {desc}",
        "filter.graph": graph,
        "capture.props": {"node.name": f"effect_input.yx_{nid}", "media.class": "Audio/Sink",
                          "node.description": f"音效 · {desc}",
                          "audio.channels": 2, "audio.position": ["FL", "FR"]},
        "playback.props": {"node.name": f"effect_output.yx_{nid}", "node.passive": True,
                           "audio.channels": 2, "audio.position": ["FL", "FR"]},
    }
    return {"context.modules": [{"name": "libpipewire-module-filter-chain", "args": chain}]}


def preamp_for(graph):
    """整条链的最坏增益上界 → 带 0.25dB 余量向上取到 0.25dB 步进的负增益。

    用 dsp.reverb_bound 的解析上界，而不是曲线上的 σmax ——
    理由见 dsp.reverb_bound 的注释（噪声型 IR 的网格采样误差会骗人）。
    """
    g = json.loads(json.dumps(graph))
    for n in g["nodes"]:
        if n["name"] in ("preL", "preR"):
            n["control"]["Gain"] = 0.0
    b = dsp.reverb_bound(g)
    assert b is not None, "不是混响型图"
    return -math.ceil((b + 0.25) * 4) / 4.0, 10 ** (b / 20.0)


def side_rms(graph, mode, wet, centre=1000.0):
    """干路或湿路的 1/3 倍频程 RMS（dB）。preamp 归零，只看链路本身。"""
    g = json.loads(json.dumps(graph))
    for n in g["nodes"]:
        if n["name"] in ("mL", "mR"):
            if mode == "dry":
                n["control"]["Gain 2"] = 0.0
            else:
                n["control"]["Gain 1"] = 0.0
                n["control"]["Gain 2"] = wet
        if n["name"] in ("preL", "preR"):
            n["control"]["Gain"] = 0.0
    a = dsp.analyze(g)
    idx = [i for i, f in enumerate(a["freqs"]) if centre / 1.26 <= f <= centre * 1.26]
    p = sum(10 ** (a["ch_db"][i] / 10.0) for i in idx) / max(len(idx), 1)
    return 10 * math.log10(max(p, 1e-30))


def solve_wet(graph, target_db, centre=1000.0):
    """解出满足目标干湿比的 wet 增益：湿路对 wet 是线性的，一次求值就够。"""
    dry = side_rms(graph, "dry", 0.0, centre)
    wet1 = side_rms(graph, "wet", 1.0, centre)
    return 10 ** (target_db / 20.0) * 10 ** (dry / 20.0) / 10 ** (wet1 / 20.0)


def wet_ratio_db(graph, wet, centre=1000.0):
    """校验：按算出来的 wet 反算实际干湿比，应当等于目标值。"""
    return side_rms(graph, "wet", wet, centre) - side_rms(graph, "dry", 0.0, centre)


def main():
    dry_run = "--dry-run" in sys.argv
    out = "/tmp/yxreverb" if dry_run else CONFDIR
    wet_override = None
    if "--wet" in sys.argv:
        wet_override = float(sys.argv[sys.argv.index("--wet") + 1])
    os.makedirs(out, exist_ok=True)
    hw = open(HW).read().strip() if os.path.exists(HW) and not dry_run else ""

    print(f"{'预设':<14}{'RT60':>7}{'预延迟':>8}{'湿比':>7}{'σmax(preamp=0)':>15}{'自动preamp':>12}{'1kHz 湿/干':>12}{'削顶余量':>10}")
    print("-" * 96)
    rows = []
    for k, h in enumerate(dsp_ir_halls()):
        ir_wav = os.path.join(IRDIR, h["id"] + ".wav")
        if not os.path.exists(ir_wav):
            print(f"{h['id']:<14}  缺 IR：{ir_wav}（先跑 gen_irs.py）")
            continue
        g0 = reverb_graph(ir_wav, h["tone"], 1.0)
        wet = wet_override if wet_override is not None else solve_wet(g0, h["wet_db"])
        g = reverb_graph(ir_wav, h["tone"], round(wet, 4))
        preamp, sig = preamp_for(g)
        ratio = wet_ratio_db(g, round(wet, 4))
        for n in g["nodes"]:
            if n["name"] in ("preL", "preR"):
                n["control"]["Gain"] = preamp
        idx = START_IDX + k
        path = f"{out}/50-yinxiao-{idx:02d}-{h['id']}.conf"
        body = conf(h["id"], h["name"], g)
        if hw:
            body["context.modules"][0]["args"]["playback.props"]["node.target"] = hw
        with open(path, "w") as f:
            f.write(f"# yinxiao preset: {h['id']}  (reverb, IR {os.path.basename(ir_wav)}, "
                    f"RT60 {h['rt60']}s, preamp {preamp:+.2f}dB)\n")
            f.write(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
        rows.append((h["id"], h["name"], h["note"], preamp))
        print(f"{h['id']:<14}{h['rt60']:>6.2f}s{h['pre_ms']:>7.0f}ms{wet:>7.2f}"
              f"{sig:>14.3f}{preamp:>11.2f}dB{ratio:>11.1f}dB"
              f"{-20 * math.log10(max(sig, 1e-9)):>12.2f}dB")
    if dry_run:
        print(f"\n[dry-run] 已写到 {out}，未改动系统配置与 presets.tsv")
        return
    # 更新清单：混响预设追加（已存在则替换同名行）
    lines = open(TSV, encoding="utf-8").read().splitlines()
    have = {l.split("\t")[0] for l in lines if l and not l.startswith("#")}
    for pid, name, note, preamp in rows:
        row = f"{pid}\t{name}\t-\t{note}"
        if pid in have:
            lines = [row if l.split("\t")[0] == pid else l for l in lines]
        else:
            lines.append(row)
    with open(TSV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n已写入 {len(rows)} 个混响预设 → {out}")
    print("生效需要: systemctl --user restart pipewire pipewire-pulse wireplumber")


def dsp_ir_halls():
    """从 gen_irs.py 读厅堂定义，保证 IR 与预设参数同源。"""
    spec = importlib.util.spec_from_file_location("gen_irs", os.path.join(HERE, "gen_irs.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.HALLS


if __name__ == "__main__":
    main()
