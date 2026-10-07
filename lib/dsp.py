#!/usr/bin/env python3
"""yinxiao 共享 DSP 内核。
这里的公式与 eval_graph.py / gen_presets.py 完全一致 —— 已用端到端实测验证
（26 个测点平均偏差 0.062 dB）。编辑器复用同一份实现，保证"看到的曲线"就是
"实际听到的曲线"。
"""
import cmath, json, math, os, re, subprocess, glob

FS = 48000.0
CONFDIR = os.path.expanduser("~/.config/pipewire/pipewire.conf.d")
PREFIX = "effect_input.yx_"
IRDIR = os.path.expanduser("~/.local/share/yinxiao/ir")

# 求值网格：20Hz-20kHz 1/48 倍频程（479 点）。
# 为什么这么密：混响 IR 是噪声型信号，频谱本身起伏很快，
# 网格太粗会漏掉真实峰值 —— σmax 的“不削顶”结论会失真。
FGRID = [20.0 * (1000.0 ** (i / 478.0)) for i in range(479)]

# ---------------------------------------------------------------- RBJ 双二阶
def biquad(label, f0, Q, gain_db):
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / FS
    cw, sw = math.cos(w0), math.sin(w0)
    if label == "bq_peaking":
        al = sw / (2 * Q)
        return [1 + al*A, -2*cw, 1 - al*A], [1 + al/A, -2*cw, 1 - al/A]
    if label in ("bq_lowshelf", "bq_highshelf"):
        al = sw / 2 * math.sqrt((A + 1/A) * (1/Q - 1) + 2)
        sq = 2 * math.sqrt(A) * al
        if label == "bq_lowshelf":
            return ([A*((A+1)-(A-1)*cw+sq), 2*A*((A-1)-(A+1)*cw), A*((A+1)-(A-1)*cw-sq)],
                    [(A+1)+(A-1)*cw+sq, -2*((A-1)+(A+1)*cw), (A+1)+(A-1)*cw-sq])
        return ([A*((A+1)+(A-1)*cw+sq), -2*A*((A-1)+(A+1)*cw), A*((A+1)+(A-1)*cw-sq)],
                [(A+1)-(A-1)*cw+sq, 2*((A-1)-(A+1)*cw), (A+1)-(A-1)*cw-sq])
    if label == "bq_highpass":
        al = sw/(2*Q); return [(1+cw)/2, -(1+cw), (1+cw)/2], [1+al, -2*cw, 1-al]
    if label == "bq_lowpass":
        al = sw/(2*Q); return [(1-cw)/2, 1-cw, (1-cw)/2], [1+al, -2*cw, 1-al]
    return None

# ----------------------------------------------------- 卷积混响 IR
# 混响型预设用 builtin convolver 加载 WAV 脉冲响应。这里把 IR 的频响算一次就缓存
# 成 <ir>.resp.json（FGRID 240 点复数），之后编辑器/σmax 校验都是秒读；
# 缓存缺失或被删时现场算一份（纯 Python FFT，慢但不用任何第三方库）。
_IR_MEMO = {}


def _fft(re, im):
    """迭代式 radix-2 FFT（就地），长度必须是 2 的幂。"""
    n = len(re)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit; bit >>= 1
        j |= bit
        if i < j:
            re[i], re[j] = re[j], re[i]
            im[i], im[j] = im[j], im[i]
    ln = 2
    while ln <= n:
        ang = -2 * math.pi / ln
        wr, wi = math.cos(ang), math.sin(ang)
        half = ln >> 1
        for i in range(0, n, ln):
            cr, ci = 1.0, 0.0
            for k in range(half):
                ur, ui = re[i + k], im[i + k]
                vr = re[i + k + half] * cr - im[i + k + half] * ci
                vi = re[i + k + half] * ci + im[i + k + half] * cr
                re[i + k], im[i + k] = ur + vr, ui + vi
                re[i + k + half], im[i + k + half] = ur - vr, ui - vi
                cr, ci = cr * wr - ci * wi, cr * wi + ci * wr
        ln <<= 1


def _spectrum(samples, freqs):
    """IR 在给定频点上的复数频响（零填充 + FFT，取最近频点）。"""
    n = len(samples)
    m = 1
    while m < n:
        m <<= 1
    try:
        import numpy as np
        x = np.zeros(m)
        x[:n] = samples
        X = np.fft.rfft(x)
        k = np.clip(np.round(np.array(freqs) * m / FS).astype(int), 0, len(X) - 1)
        return [complex(v) for v in X[k]]
    except ImportError:
        pass
    re = list(samples) + [0.0] * (m - n)
    im = [0.0] * m
    _fft(re, im)
    out = []
    for f in freqs:
        k = int(round(f * m / FS)) % m
        out.append(complex(re[k], im[k]))
    return out


def _band_peak(samples, lo=20.0, hi=20000.0):
    """全 FFT bin 内的幅值峰值（只取 20Hz-20kHz）。"""
    n = len(samples)
    m = 1
    while m < n:
        m <<= 1
    try:
        import numpy as np
        X = np.fft.rfft(np.concatenate([np.asarray(samples), np.zeros(m - n)]))
        fr = np.fft.rfftfreq(m, 1.0 / FS)
        a, b = np.searchsorted(fr, lo), min(np.searchsorted(fr, hi) + 1, len(X))
        return float(np.max(np.abs(X[a:b]))) if b > a else 0.0
    except ImportError:
        pass
    re = list(samples) + [0.0] * (m - n)
    im = [0.0] * m
    _fft(re, im)
    a, b = int(lo * m / FS), min(int(hi * m / FS) + 1, m)
    return max((math.hypot(re[i], im[i]) for i in range(a, b)), default=0.0)


def _read_wav_stereo(path):
    """读 16bit / 24bit PCM WAV 成 [[ch0...], [ch1...]] 的浮点。"""
    import array, wave
    with wave.open(path) as w:
        ch, n, sw = w.getnchannels(), w.getnframes(), w.getsampwidth()
        raw = w.readframes(n)
    out = [[0.0] * n for _ in range(max(ch, 1))]
    if sw == 3:
        for i in range(n):
            for c in range(ch):
                k = (i * ch + c) * 3
                out[c][i] = int.from_bytes(raw[k:k + 3], "little", signed=True) / 8388608.0
        return out
    a = array.array("h")
    a.frombytes(raw[:len(raw) // 2 * 2])
    for i in range(n):
        for c in range(ch):
            out[c][i] = a[i * ch + c] / 32768.0
    return out


IR_NORM = 0.85      # 归一化目标：|H| 峰值 = -1.4dB，余量留给“连续频谱 vs 采样网格”的误差


def compute_ir_cache(path, rt60=None, pre_ms=None):
    """算 IR 的频响并按「全带峰值 = 0.95」归一化，写 <ir>.resp.json。

    0.95 而不是 1.0：留给采样点之间的真实峰值 0.45dB 余量，
    这样下游可以放心断言 |H(f)| ≤ 1，把它当成“单位增益的最坏情况”。
    """
    ch = _read_wav_stereo(path)
    if len(ch) == 1:
        ch = [ch[0], ch[0]]
    h = [_spectrum(ch[0], FGRID), _spectrum(ch[1], FGRID)]
    # 归一化基准取「全 FFT bin 在 20Hz-20kHz 内的最大值」，而不是采样网格上的最大值：
    # 网格只有 479 个点，采样的最大值可能比真实峰值低 1~2dB。
    peak_full = max(_band_peak(ch[0]), _band_peak(ch[1])) or 1.0
    g = IR_NORM / peak_full
    h = [[v * g for v in row] for row in h]
    peak = peak_full * g
    k1k = min(range(len(FGRID)), key=lambda i: abs(FGRID[i] - 1000.0))
    mid_db = 20 * math.log10(max(abs(h[0][k1k]), abs(h[1][k1k])) / 0.95 + 1e-12)
    data = {"file": os.path.basename(path), "fs": FS, "frames": len(ch[0]),
            "rt60": rt60, "pre_ms": pre_ms,
            "norm_gain": g, "peak_before": peak, "mid_1k_db": mid_db,
            "freqs": FGRID,
            "h0": [[v.real, v.imag] for v in h[0]],
            "h1": [[v.real, v.imag] for v in h[1]]}
    try:
        with open(path + ".resp.json", "w") as f:
            json.dump(data, f)
    except OSError:
        pass
    return data


def ir_response(path):
    """返回 {"h0": {频率: 复数}, "h1": {...}, "rt60":..., "file":...}（带内存缓存）。"""
    try:
        st = os.stat(path)
    except OSError:
        return {"h0": {}, "h1": {}, "rt60": None, "file": path, "missing": True}
    key = (path, st.st_mtime_ns, st.st_size)
    if key in _IR_MEMO:
        return _IR_MEMO[key]
    data = None
    try:
        d = json.load(open(path + ".resp.json", encoding="utf-8"))
        if d.get("freqs") == FGRID and len(d.get("h0", [])) == len(FGRID):
            data = d
    except Exception:
        pass
    if data is None:
        data = compute_ir_cache(path)
    out = {"h0": {f: complex(r, i) for f, (r, i) in zip(FGRID, data["h0"])},
           "h1": {f: complex(r, i) for f, (r, i) in zip(FGRID, data["h1"])},
           "rt60": data.get("rt60"), "pre_ms": data.get("pre_ms"),
           "file": os.path.basename(path),
           "mid_1k_db": data.get("mid_1k_db")}
    _IR_MEMO[key] = out
    return out


def ir_at(r, f, channel=0):
    """取 IR 在 f 处的复数响应；f 不在网格上就取最近点。"""
    tbl = r["h0"] if int(channel) == 0 else r["h1"]
    if not tbl:
        return 0j
    if f in tbl:
        return tbl[f]
    k = min(tbl, key=lambda x: abs(x - f))
    return tbl[k]


def node_H(node, f):
    label, c = node["label"], node.get("control", {})
    if label == "copy":
        return 1 + 0j
    if label == "delay":
        # builtin delay: H = feedforward·z⁻ᵗ / (1 - feedback·z⁻ᵗ)
        tau = float(c.get("Delay (s)", 0.0))
        z = cmath.exp(-2j * math.pi * f * tau)
        ff = 10 ** (float(c.get("Feedforward", 0.0)) / 20.0)
        fb = 10 ** (float(c.get("Feedback", 0.0)) / 20.0)
        return ff * z / (1 - fb * z) if fb else ff * z
    if label == "convolver":
        cfg = node.get("config") or {}
        r = ir_response(cfg.get("filename", ""))
        return ir_at(r, f, cfg.get("channel", 0)) * float(cfg.get("gain", 1.0))
    r = biquad(label, float(c.get("Freq", 1000.0)), float(c.get("Q", 0.7)),
               float(c.get("Gain", 0.0)))
    if r is None:
        return 1 + 0j
    b, a = r
    z = cmath.exp(-2j * math.pi * f / FS)
    return (b[0] + b[1]*z + b[2]*z*z) / (a[0] + a[1]*z + a[2]*z*z)

# ---------------------------------------------------------------- 图求值器
class Graph:
    """按 links 拓扑递归求复频响应；支持 mixer 多输入与扇出。"""
    def __init__(self, graph):
        self.nodes = {n["name"]: n for n in graph["nodes"]}
        self.links = [(l["output"], l["input"]) for l in graph.get("links", [])]
        self.in_map = {p: i for i, p in enumerate(graph.get("inputs", []))}
        self.outs = graph.get("outputs", [])
        self.by_in = {}
        for src, dst in self.links:
            self.by_in.setdefault(dst, []).append(src)
        self.explicit = bool(graph.get("inputs"))
        if not self.explicit:          # 对称 EQ：自行定位链路首尾
            srcs = {s.split(":")[0] for s, _ in self.links}
            dsts = {d.split(":")[0] for _, d in self.links}
            st = [n for n in self.nodes if n not in dsts]
            en = [n for n in self.nodes if n not in srcs]
            if st and en:
                self.in_map = {f"{st[0]}:In": 0}
                self.outs = [f"{en[0]}:Out"]

    def eval(self, f, ext):
        memo, busy = {}, set()
        def out(port):
            if port in memo: return memo[port]
            name, _ = port.split(":")
            if name in busy: raise RuntimeError("cycle in graph")
            busy.add(name)
            node = self.nodes[name]; label = node["label"]; c = node.get("control", {})
            if label == "mixer":
                tot = 0j
                for k in range(1, 9):
                    key = f"Gain {k}"
                    if key not in c: break
                    tot += float(c[key]) * inv(name, f"In {k}")
            else:
                tot = node_H(node, f) * inv(name, "In")
            busy.discard(name); memo[port] = tot
            return tot
        def inv(name, port):
            key = f"{name}:{port}"
            if key in self.in_map:
                return ext[self.in_map[key]]
            return sum(out(s) for s in self.by_in.get(key, []))
        return [out(o) for o in self.outs]

def band_rms_db(db_arr, grid, centre=1000.0):
    """1/3 倍频程带内 RMS（dB）—— 单频点对噪声型 IR 没意义，带内平均才是听感。"""
    idx = [i for i, f in enumerate(grid) if centre / 1.26 <= f <= centre * 1.26]
    if not idx:
        return None
    p = sum(10 ** (db_arr[i] / 10.0) for i in idx) / len(idx)
    return 10 * math.log10(max(p, 1e-30))


def smooth_third_octave(db_arr, grid):
    """1/3 倍频程平滑（对数域功率平均）—— 混响 IR 的频谱是噪声型的，
    原始曲线一个个尖峰纯属随机起伏，平滑后才看得出音色走向。"""
    out = []
    for f in grid:
        idx = [i for i, x in enumerate(grid) if f / 1.26 <= x <= f * 1.26]
        p = sum(10 ** (db_arr[i] / 10.0) for i in idx) / max(len(idx), 1)
        out.append(10 * math.log10(max(p, 1e-30)))
    return out


def reverb_mix_db(graph, centre=1000.0):
    """混响型的「湿/干」听感比（dB，1kHz 1/3 倍频程）。

    分两次求值：把 mixer 的湿路增益归零得干路、干路增益归零得湿路。
    """
    if "mL" not in {n["name"] for n in graph["nodes"]}:
        return None
    out = []
    for mode in ("dry", "wet"):
        g = json.loads(json.dumps(graph))
        for n in g["nodes"]:
            if n["name"] in ("mL", "mR"):
                n["control"]["Gain 2" if mode == "dry" else "Gain 1"] = 0.0
        a = analyze(g)
        out.append(band_rms_db(a["ch_db"], a["freqs"], centre))
    if None in out:
        return None
    return out[1] - out[0]


def reverb_bound(graph):
    """混响型预设的「解析最坏增益上界」。

    为什么不能只看曲线上的 σmax：IR 是噪声型信号，频谱起伏很快，
    479 点网格采到的最大值可能比真实峰值低好几个 dB —— 用它归一化会偷偷削顶。
    这里改成只依赖三个**解析可知**的量：

        σmax ≤ max|EQ(f)| · (dry + x·wet·max|H_wetHP(f)|·IR 峰值)

    · max|EQ| / max|H_wetHP| 来自双二阶（平滑函数，网格采样误差 <0.05dB）
    · IR 峰值 = IR_NORM（写 WAV 时就按这个值归一化，见 gen_irs.py）
    · x 是湿路的串扰系数：每声道各自送混响（本项目的做法）→ x=1；
      若湿路是"左右汇总成一路再送两个声道"，对 L=R 的相关输入会同相叠加 → x=2
      （和 sub-woofer 的低频相关是同一个道理）。本函数两种拓扑都认。
    """
    nodes = graph["nodes"]
    by = {n["name"]: n for n in nodes}
    if "mL" not in by or not any(n["label"] == "convolver" for n in nodes):
        return None
    tone = [n for n in nodes if n["name"].startswith("tL")]
    eq_max = 1.0
    for f in FGRID:
        v = 1 + 0j
        for n in tone:
            v *= node_H(n, f)
        eq_max = max(eq_max, abs(v))
    hw_max = max((abs(node_H(by["hwL"], f)) for f in FGRID), default=1.0) if "hwL" in by else 1.0
    dry = float(by["mL"]["control"].get("Gain 1", 1.0))
    wet = float(by["mL"]["control"].get("Gain 2", 0.0))
    pre = 10 ** (float(by["preL"]["control"].get("Gain", 0.0)) / 20.0) if "preL" in by else 1.0
    mono_send = any(n["name"] == "mono" and n["label"] == "mixer" for n in nodes)
    x = 2.0 if mono_send else 1.0
    bound = eq_max * (dry + x * wet * hw_max * IR_NORM) * pre
    return 20 * math.log10(max(bound, 1e-9))


def sigma_max(M):
    a, b = M[0]; c, d = M[1]
    n2 = abs(a)**2 + abs(b)**2 + abs(c)**2 + abs(d)**2
    det = a*d - b*c
    disc = max(0.0, n2*n2 - 4*abs(det)**2)
    return math.sqrt(max(0.0, (n2 + math.sqrt(disc)) / 2))

def analyze(graph, grid=None):
    """返回曲线的 dB 数组、σmax、以及单声道响应。"""
    grid = grid or FGRID
    g = Graph(graph)
    nin = len(graph.get("inputs", [])) or 1
    sig, ch, mono, corr, side = [], [], [], [], []
    for f in grid:
        cols = []
        for j in range(nin):
            ext = [1+0j if i == j else 0j for i in range(nin)]
            cols.append(g.eval(f, ext))
        if nin == 1:
            h = cols[0][0]
            M = [[h, 0j], [0j, h]]
            ch.append(20*math.log10(abs(h)) if abs(h) > 1e-12 else -120)
            mono.append(ch[-1]); corr.append(ch[-1]); side.append(ch[-1])
        else:
            M = [[cols[0][0], cols[1][0]], [cols[0][1], cols[1][1]]]
            ch.append(20*math.log10(abs(cols[0][0])) if abs(cols[0][0]) > 1e-12 else -120)
            ms = M[0][0] + M[1][0]
            mono.append(20*math.log10(abs(ms)) if abs(ms) > 1e-12 else -120)
            # 输入 L=R=1（居中/单声道内容）时左声道输出
            c = M[0][0] + M[0][1]
            corr.append(20*math.log10(abs(c)) if abs(c) > 1e-12 else -120)
            # 输入 L=1,R=-1（侧向内容）时左声道输出
            s = M[0][0] - M[0][1]
            side.append(20*math.log10(abs(s)) if abs(s) > 1e-12 else -120)
        sig.append(20*math.log10(max(sigma_max(M), 1e-12)))
    return {"freqs": grid, "sigma_db": sig, "ch_db": ch, "mono_db": mono,
            "corr_db": corr, "side_db": side,
            "sigma_max": max(10 ** (v/20) for v in sig)}

# ---------------------------------------------------------------- conf 读写
def strip_comments(txt):
    return re.sub(r'^\s*#.*$', '', txt, flags=re.M)

def conf_files():
    return sorted(glob.glob(f"{CONFDIR}/50-yinxiao-*.conf"))

def preset_id_of(path):
    return re.sub(r'\.conf$', '', os.path.basename(path).split("-", 3)[3])

def load_graph(path):
    body = json.loads(strip_comments(open(path, encoding="utf-8").read()))
    return body["context.modules"][0]["args"]

def save_graph(path, args, note=""):
    """原子写回，保留一份 .bak。内容无实质变化时不写盘、也不动 .bak，
    否则一次"空保存"就会把真正的原始备份冲掉。"""
    body = {"context.modules": [{"name": "libpipewire-module-filter-chain", "args": args}]}
    if os.path.exists(path):
        try:
            if json.loads(strip_comments(open(path, encoding="utf-8").read())) == body:
                return False        # 无变化
        except Exception:
            pass
        try: os.replace(path, path + ".bak")
        except OSError: pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if note:
            f.write(f"# {note}\n")
        f.write(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)
    return True

# ---------------------------------------------------------------- 预设目录
CHAIN_KINDS = {"bq_highpass": "HP", "bq_lowpass": "LP", "bq_lowshelf": "LS",
               "bq_highshelf": "HS", "bq_peaking": "PK"}

def describe(args):
    """把 conf 转成前端友好的结构。"""
    graph = args["filter.graph"]
    nodes = graph["nodes"]
    preamp = None
    bands = []
    for n in nodes:
        lb = n["label"]
        if n["name"] in ("hwL", "hwR") or n["name"].startswith("pre"):
            # 湿路高通与 preamp 由混响面板单独管，不混进 bands 列表
            if n["name"] in ("pre", "preL"):
                preamp = float(n.get("control", {}).get("Gain", 0.0))
            continue

        if lb in CHAIN_KINDS:
            c = n.get("control", {})
            bands.append({"node": n["name"], "kind": CHAIN_KINDS[lb], "label": lb,
                          "freq": float(c.get("Freq", 1000.0)),
                          "q": float(c.get("Q", 0.7)),
                          "gain": float(c.get("Gain", 0.0))})
    kind = "eq"
    mat = None
    rev = None
    conv = [n for n in nodes if n["label"] == "convolver"]
    if conv:
        # 混响型：湿/干比例直接读 mixer，预延迟读 delay 节点，RT60 来自 IR 缓存
        byname = {n["name"]: n for n in nodes}
        kind = "reverb"
        wet = dry = 0.0
        if "mL" in byname:
            mc = byname["mL"].get("control", {})
            dry, wet = float(mc.get("Gain 1", 1.0)), float(mc.get("Gain 2", 0.0))
        cfg = conv[0].get("config") or {}
        info = ir_response(cfg.get("filename", ""))
        rev = {"wet": round(wet, 6), "dry": round(dry, 6),
               "predelay_ms": info.get("pre_ms"),          # 预延迟烘在 IR 里
               "rt60": info.get("rt60"), "ir": info.get("file"),
               "wet_hp": float(byname["hwL"]["control"].get("Freq", 0.0)) if "hwL" in byname else None,
               "mid_1k_db": info.get("mid_1k_db")}
        return {"kind": kind, "bands": bands, "preamp": preamp, "matrix": mat, "reverb": rev}
    if graph.get("inputs"):
        names = {n["name"] for n in nodes}
        if {"wL", "wR"} <= names:
            kind = "widen"
            c = [n for n in nodes if n["name"] == "wL"][0]["control"]
            g1, g2 = float(c["Gain 1"]), float(c["Gain 2"])
            g = g1 + g2                     # = g(1+k) + (-gk)
            mat = {"k": round(g1/g - 1, 6) if g else 0.0, "g": round(g, 6)}
        elif {"mix", "lp", "fL"} <= names:
            kind = "sub"
            fL = [n for n in nodes if n["name"] == "fL"][0]["control"]
            mat = {"sub": round(float(fL["Gain 2"]) / float(fL["Gain 1"]), 6),
                   "xfreq": float([n for n in nodes if n["name"] == "lp"][0]["control"]["Freq"])}
    return {"kind": kind, "bands": bands, "preamp": preamp, "matrix": mat, "reverb": rev}

def apply_describe(args, spec):
    """把前端的修改写回 graph（内存内）。"""
    graph = args["filter.graph"]
    byname = {n["name"]: n for n in graph["nodes"]}
    for b in spec.get("bands", []):
        n = byname.get(b["node"])
        if not n: continue
        n.setdefault("control", {})
        n["control"]["Freq"] = round(float(b["freq"]), 3)
        n["control"]["Q"]    = round(float(b["q"]), 4)
        n["control"]["Gain"] = round(float(b["gain"]), 3)
    if spec.get("preamp") is not None:
        for nm in ("pre", "preL", "preR"):          # 单声道 pre / 立体声 preL+preR
            if nm in byname:
                byname[nm]["control"]["Gain"] = round(float(spec["preamp"]), 3)
    rv = spec.get("reverb")
    if rv and "mL" in byname:
        if rv.get("wet") is not None:
            w = round(float(rv["wet"]), 6)
            byname["mL"]["control"]["Gain 2"] = w
            byname["mR"]["control"]["Gain 2"] = w
        if rv.get("dry") is not None:
            d = round(float(rv["dry"]), 6)
            byname["mL"]["control"]["Gain 1"] = d
            byname["mR"]["control"]["Gain 1"] = d
        if rv.get("predelay_ms") is not None and "pd" in byname:
            byname["pd"]["control"]["Delay (s)"] = round(float(rv["predelay_ms"]) / 1000.0, 6)
    m = spec.get("matrix")
    if m:
        if "k" in m and {"wL", "wR"} <= set(byname):
            k = float(m["k"]); g = 1.0 / max(1.0, abs(1 + 2*k))
            byname["wL"]["control"] = {"Gain 1": round(g*(1+k), 6), "Gain 2": round(-g*k, 6)}
            byname["wR"]["control"] = {"Gain 1": round(-g*k, 6), "Gain 2": round(g*(1+k), 6)}
        if "sub" in m and "fL" in byname:
            s = float(m["sub"]); g = 1.0 / (1.0 + s)
            byname["fL"]["control"] = {"Gain 1": round(g, 6), "Gain 2": round(g*s, 6)}
            byname["fR"]["control"] = {"Gain 1": round(g, 6), "Gain 2": round(g*s, 6)}
        if "xfreq" in m and "lp" in byname:
            byname["lp"]["control"]["Freq"] = round(float(m["xfreq"]), 3)

def controls_of(args):
    """铺平成 { "node:Control": value } ，供 pw-cli 实时下发。"""
    out = {}
    for n in args["filter.graph"]["nodes"]:
        for k, v in (n.get("control") or {}).items():
            if isinstance(v, (int, float)):
                out[f"{n['name']}:{k}"] = float(v)
    return out

# ---------------------------------------------------------------- 运行时对接
def node_ids():
    """动态查 node.name -> id（PipeWire 重启后 id 会变，不能缓存写死）。"""
    try:
        raw = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10).stdout
        out = {}
        for o in json.loads(raw):
            if o.get("type") == "PipeWire:Interface:Node":
                nm = o["info"].get("props", {}).get("node.name", "")
                if nm.startswith(PREFIX):
                    out[nm] = o["id"]
        return out
    except Exception:
        return {}

def live_set(node_id, sets):
    """把 control 值实时推给正在运行的 filter-chain（无需重启）。"""
    items = " ".join(f'"{k}" {float(v)}' for k, v in sets.items())
    r = subprocess.run(["pw-cli", "s", str(node_id), "Props",
                        "{ params = [ %s ] }" % items],
                       capture_output=True, text=True, timeout=10)
    return r.returncode == 0, (r.stderr or "").strip()

def active_sink():
    try:
        return subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return ""

def list_presets():
    out = []
    for p in conf_files():
        pid = preset_id_of(p)
        args = load_graph(p)
        d = describe(args)
        d["id"] = pid
        d["path"] = p
        d["name"] = args.get("node.description", pid).replace("音效 · ", "").replace("音效·", "")
        d["desc"] = ""
        out.append(d)
    # 从清单补齐中文描述
    try:
        for line in open(os.path.expanduser("~/.local/share/yinxiao/presets.tsv"), encoding="utf-8"):
            if line.startswith("#") or not line.strip(): continue
            f = line.rstrip("\n").split("\t")
            for d in out:
                if d["id"] == f[0]:
                    d["desc"] = f[3] if len(f) > 3 else ""
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- 结构编辑
def insert_band(args, freq=1000.0, q=1.0, gain=0.0):
    """在 preamp 之前插入一段峰值滤波器，并接好链路。"""
    graph = args["filter.graph"]
    nodes = graph["nodes"]
    if "pre" not in {n["name"] for n in nodes}:
        why = "混响型" if any(n["label"] == "convolver" for n in nodes) else "矩阵型"
        return None, f"该预设（{why}）不支持直接用滤波器段改音色"
    prev = None
    for l in graph.get("links", []):
        if l["input"] == "pre:In":
            prev = l["output"]; break
    if prev is None:
        return None, "找不到 preamp 的上游"
    used = {n["name"] for n in nodes}
    i = 1
    while f"pk{i}" in used:
        i += 1
    name = f"pk{i}"
    idx = [k for k, n in enumerate(nodes) if n["name"] == "pre"][0]
    nodes.insert(idx, {"type": "builtin", "name": name, "label": "bq_peaking",
                       "control": {"Freq": float(freq), "Q": float(q), "Gain": float(gain)}})
    graph["links"] = [l for l in graph["links"] if l["input"] != "pre:In"]
    graph["links"].append({"output": prev, "input": f"{name}:In"})
    graph["links"].append({"output": f"{name}:Out", "input": "pre:In"})
    return name, None

def remove_band(args, name):
    graph = args["filter.graph"]
    nodes = graph["nodes"]
    if name == "pre" or name not in {n["name"] for n in nodes}:
        return False, "不能删除该节点"
    src = dst = None
    for l in graph.get("links", []):
        if l["input"] == f"{name}:In":  src = l["output"]
        if l["output"] == f"{name}:Out": dst = l["input"]
    if src is None or dst is None:
        return False, "该节点在链路首尾，删掉会断开链路"
    graph["links"] = [l for l in graph["links"]
                      if l["output"].split(":")[0] != name and l["input"].split(":")[0] != name]
    graph["links"].append({"output": src, "input": dst})
    nodes.remove([n for n in nodes if n["name"] == name][0])
    return True, None


def app_sink_inputs():
    """只返回"应用"的播放流索引。

    必须排除 yinxiao 自己的 effect_output.* 预设输出流 —— 把它们搬走会破坏
    node.target 钉定，导致音频被路由到别的设备（曾经因此整机没声音）。
    """
    try:
        raw = subprocess.run(["pactl", "list", "sink-inputs"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return []
    out, cur, skip = [], None, False
    for line in raw.splitlines():
        m = re.match(r'^Sink Input #(\d+)\s*$', line)
        if m:
            if cur is not None and not skip:
                out.append(cur)
            cur, skip = m.group(1), False
            continue
        if cur is not None and ('node.name = "effect_output.' in line
                                or 'node.passive = "true"' in line):
            skip = True
    if cur is not None and not skip:
        out.append(cur)
    return out
