#!/usr/bin/env python3
"""yinxiao 厅堂脉冲响应（IR）合成器 —— 纯 Python，无第三方依赖。

生成 4 个立体声 IR（48kHz / 24bit WAV），每个都把「早反射 + 指数衰减尾巴 +
空气吸收」直接烘进 IR 本身，所以预设链路里只需要一个 convolver，不需要额外的
反馈延迟网络去凑混响感。

数学上做了两件保证：
  1. 每个通道的 IR 都按「全 FFT bin 频谱峰值 = 0.85」归一化（留 1.4dB 余量给
     "连续频谱 vs 采样网格"的误差），而且这个增益**真的写进 WAV**，
     于是 convolver 的 |H(f)| ≤ 1 —— 湿路增益 w 就是它的最坏情况占比，
     预设的 σmax 可以直接解析算出，仍然满足 σmax ≤ 1 不削顶。
  2. 频响缓存（*.resp.json）里存的是 dsp.FGRID（1/48 倍频程，479 点）上的复数响应，
     与 dsp.py / 编辑器的求值网格完全一致 —— 「看到的曲线 = 听到的曲线」。

用法:
    python3 gen_irs.py                 # 生成到 ~/.local/share/yinxiao/ir/
    python3 gen_irs.py --dir /tmp/ir   # 换个输出目录
"""
import math, os, random, struct, sys, wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dsp

FS = 48000
OUTDIR = os.path.expanduser("~/.local/share/yinxiao/ir")

# ---------------------------------------------------------------- 厅堂参数
# rt60     : 中频混响时间（高频按 air 系数缩短、低频按 low 系数延长）
# dur      : IR 总长（秒），取 rt60 再加一点尾巴
# er_*     : 早反射窗口（秒）与密度
# wet_db   : 目标干湿比（1kHz 处湿路相对干路的 1/3 倍频程电平，dB）
# tone     : 干路整体音色 [(标签, 频率, Q, 增益)]，HP 用 Q 占位
# pre_ms   : 预延迟（毫秒）—— 在 IR 开头留一段静音，物理上就是直达声到早反射的间隔
HALLS = [
    dict(id="live-concert", name="演唱会", seed=11,
         rt60=1.10, dur=1.35, low=1.15, air=0.55, er_lo=0.004, er_hi=0.140,
         er_min=0.007, er_max=0.021, er_decay=0.055, er_gain=0.70, diffuseness=0.85,
         wet_db=-11.0, pre_ms=18,
         tone=[("HP", 34, 0.7, 0.0)],
         note="体育馆中段，早反射密集、鼓点有力，人声有厅堂包围感"),
    dict(id="hall-orchestra", name="音乐厅", seed=23,
         rt60=2.20, dur=2.55, low=1.20, air=0.45, er_lo=0.008, er_hi=0.120,
         er_min=0.010, er_max=0.030, er_decay=0.070, er_gain=0.55, diffuseness=0.80,
         wet_db=-10.0, pre_ms=26,
         tone=[("HP", 28, 0.7, 0.0), ("PK", 250, 1.0, -1.0), ("HS", 12000, 0.7, 1.0)],
         note="大型交响厅，尾巴长、高频带空气吸收，乐器前后层次清楚"),
    dict(id="livehouse", name="Live House", seed=37,
         rt60=0.60, dur=0.80, low=1.10, air=0.60, er_lo=0.002, er_hi=0.090,
         er_min=0.004, er_max=0.012, er_decay=0.035, er_gain=0.85, diffuseness=0.90,
         wet_db=-13.0, pre_ms=9,
         tone=[("HP", 40, 0.7, 0.0), ("PK", 300, 1.0, -1.5), ("PK", 2800, 1.2, 1.5),
               ("HS", 9500, 0.7, 1.0)],
         note="小场子舞台前排：混响短、直达声近，人声前凸、鼓不糊"),
    dict(id="cathedral", name="大教堂", seed=53,
         rt60=3.50, dur=4.00, low=1.25, air=0.35, er_lo=0.020, er_hi=0.260,
         er_min=0.015, er_max=0.045, er_decay=0.120, er_gain=0.45, diffuseness=0.75,
         wet_db=-9.0, pre_ms=42,
         tone=[("HP", 24, 0.7, 0.0), ("PK", 200, 1.0, -1.5), ("HS", 7000, 0.7, -1.5)],
         note="超长混响（RT60 约 3.5s），声场极宽，庄严、有石造空间感"),
]


# ---------------------------------------------------------------- 小工具
def one_pole_lp(x, cutoff):
    """一阶低通（生成用，够近似就行）。"""
    a = math.exp(-2 * math.pi * cutoff / FS)
    y, prev = [], 0.0
    for v in x:
        prev = (1 - a) * v + a * prev
        y.append(prev)
    return y


def one_pole_hp(x, cutoff):
    lp = one_pole_lp(x, cutoff)
    return [v - l for v, l in zip(x, lp)]


def dc_block_hp(x, cutoff=22.0):
    """直流/次声切除：去掉尾巴里会让功放白费力气的能量。"""
    a = math.exp(-2 * math.pi * cutoff / FS)
    y, xp, yp = [], 0.0, 0.0
    for v in x:
        cur = v - xp + a * yp
        y.append(cur); xp, yp = v, cur
    return y


def early_reflections(h, rng):
    """一通道的早反射：泊松式随机到达 + 指数衰减幅度，带随机极性。"""
    taps = []
    t = h["er_lo"]
    while t < h["er_hi"]:
        # 幅度随到达时间指数衰减，极性随机（真实房间里墙面反射有正有负）
        amp = h["er_gain"] * math.exp(-t / h["er_decay"]) * rng.uniform(0.6, 1.4)
        taps.append((t + rng.uniform(-0.0015, 0.0015), amp * (1 if rng.random() < 0.5 else -1)))
        t += rng.uniform(h["er_min"], h["er_max"])
    return taps


def bq_filter(x, label, f0, Q):
    """RBJ 双二阶（与 dsp.py 同公式），直接型 II 实现。"""
    A = 10 ** (0.0 / 40.0)
    w0 = 2 * math.pi * f0 / FS
    cw, sw = math.cos(w0), math.sin(w0)
    al = sw / (2 * Q)
    if label == "hp":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        a = [1 + al, -2 * cw, 1 - al]
    else:
        raise ValueError(label)
    b = [v / a[0] for v in b]; a = [v / a[0] for v in a]
    y, x1, x2, y1, y2 = [], 0.0, 0.0, 0.0, 0.0
    for v in x:
        cur = b[0] * v + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
        y.append(cur)
        x2, x1 = x1, v
        y2, y1 = y1, cur
    return y


def tail(h, rng, n):
    """指数衰减的扩散尾巴：分三段不同 RT60，模拟低频拖尾 + 高频空气吸收。

    用 K 个相互独立的噪声实現叠加（而非单个噪声），因为单实現的频谱纹波很大
    （±10dB），归一化时会被偶然的尖峰主导；叠加后纹波降为约 1/√K，
    归一化才能真实反映“中频混响电平”。
    """
    K = 24
    out = [0.0] * n
    for _ in range(K):
        noise = [rng.random() * 2 - 1 for _ in range(n)]
        # 频段切分（一阶近似）：低频 ≤300Hz、高频 ≥3.5kHz，其余算中频
        low = one_pole_lp(noise, 300)
        hi = one_pole_hp(noise, 3500)
        mid = [a - b for a, b in zip(noise, [l + hh for l, hh in zip(low, hi)])]
        # low/mid/high 三段的 RT60 与补偿增益
        bands = ((low, h["rt60"] * h["low"], 1.25),
                 (mid, h["rt60"], 1.00),
                 (hi,  h["rt60"] * h["air"], 0.95))
        for band, rt, boost in bands:
            tau = rt / 6.9077                  # 衰减到 -60dB 的时间常数
            g = boost / math.sqrt(K)
            for i, v in enumerate(band):
                out[i] += v * g * math.exp(-i / (tau * FS))
    return out


def synth_channel(h, rng, n, other, offset=0):
    """合成一个声道的 IR；other 是另一声道已生成的尾巴（用于控制声道间相关度）。

    offset：预延迟（采样）。直接焦进 IR 而不是用 builtin delay 节点——
    PipeWire 的 delay 节点把 Feedback/Feedforward 钳在 ±10dB，
    “无反馈” 取不到干净值，用它做预延迟会引入梳状染色；写进 IR 则零代价、可验。
    """
    ir = [0.0] * (offset + n)
    for t, a in early_reflections(h, rng):
        k = offset + int(t * FS)
        if 0 <= k < len(ir):
            ir[k] += a
    start = int(0.006 * FS)
    tl = tail(h, rng, n - start)
    # 保留一点串扰：完全独立的双声道尾巴虽然宽，但听起来不像同一个厅
    x = 1.0 - h["diffuseness"]
    for i, v in enumerate(tl):
        j = offset + start + i
        ir[j] += v + (x * other[j] if other and j < len(other) else 0.0)
    # 次声切除：低频拖尾的“嗡嗡”能量不参与听感，却白吃削顶余量
    return bq_filter(dc_block_hp(ir), "hp", 45.0, 0.7)


def spec_limit(x, ratio=1.5, alpha=0.6, lo=60.0, hi=8000.0):
    """零相位频谱限幅：把超过 T = ratio×中位数 的个别尖峰压下去。

    为什么需要：混响 IR 是噪声型信号，单个 FFT bin 的幅值起伏能有 ±5dB，
    低频段尤其明显（1.4s 的窗只有 ~2 个自由度）。这些尖峰窄到听不出来，
    却把 σmax 顶得很难看 —— 归一化时白白吃掉十几 dB 的中频混响量。
    压掉之后，同样的削顶预算能换来大得多的“听得见的混响”。

    alpha=1 是硬限幅（把超阈值的点压到阈值），alpha<1 是软膝压缩（越超压得越多、
    但保留一点起伏）。r=1.5 / α=0.6 是实测折中：比不限幅多换回约 4dB 的
    中频混响量，又不至于把噪声型尾巴压成“平顶”。
    只做幅度压缩、不动相位（零相位），引入的预振铃落在 IR 开头的预延迟静音里。
    返回 (样本, 中位数, 阈值, 被压的点数)。
    """
    n = len(x)
    m = 1
    while m < n:
        m <<= 1
    try:
        import numpy as np
        X = np.fft.rfft(np.concatenate([np.asarray(x), np.zeros(m - n)]))
        mag = np.abs(X)
        fr = np.fft.rfftfreq(m, 1.0 / FS)
        a, b = np.searchsorted(fr, lo), np.searchsorted(fr, hi)
        med = float(np.median(mag[a:b])) or 1e-30
        T = ratio * med
        g = np.minimum(1.0, (T / np.maximum(mag, 1e-30)) ** alpha)
        y = np.fft.irfft(X * g, m)[:n]
        return [float(v) for v in y], med, T, int((mag > T).sum())
    except ImportError:
        pass
    re = list(x) + [0.0] * (m - n)
    im = [0.0] * m
    dsp._fft(re, im)
    mag = [math.hypot(a, b) for a, b in zip(re, im)]
    band = sorted(mag[int(lo * m / FS):int(hi * m / FS)])
    med = band[len(band) // 2] or 1e-30
    T = ratio * med
    hit = 0
    for i, v in enumerate(mag):
        if v > T:
            g = (T / v) ** alpha
            re[i] *= g; im[i] *= g; hit += 1
    # 逆变换：conj → FFT → conj / m
    im = [-v for v in im]
    dsp._fft(re, im)
    y = [-v / m for v in re]
    return y[:n], med, T, hit


def write_wav(path, ir_stereo, gain):
    """写 24bit 立体声 WAV。

    必须用 24bit 而不是 16bit：IR 的绝对电平是按「频谱峰值 = 0.85」定的，
    而这个峰值比时域峰值大得多（长噪声尾巴的频谱峰值 ≈ √N × RMS），
    落到时域上峰值只有 -30dBFS 量级 —— 16bit 会在尾巴末端听出量化噪声。
    """
    n = len(ir_stereo[0])
    scale = 8388607.0
    with wave.open(path, "w") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(FS)
        buf = bytearray()
        for i in range(n):
            for ch in (0, 1):
                v = int(max(-1.0, min(1.0, ir_stereo[ch][i] * gain)) * scale)
                buf += v.to_bytes(3, "little", signed=True)
        w.writeframes(bytes(buf))


def main():
    out = OUTDIR
    if "--dir" in sys.argv:
        out = sys.argv[sys.argv.index("--dir") + 1]
    os.makedirs(out, exist_ok=True)
    print(f"IR 输出目录: {out}\n")
    print(f"{'预设':<16}{'时长':>8}{'落盘频响峰值':>14}{'1kHz落差':>10}{'RT60':>8}")
    print("-" * 70)
    for h in HALLS:
        rng = random.Random(h["seed"])
        n = int(h["dur"] * FS)
        off = int(h["pre_ms"] / 1000.0 * FS)
        ch = [None, None]
        ch[0] = synth_channel(h, rng, n, None, off)
        ch[1] = synth_channel(h, rng, n, ch[0], off)
        lim = [spec_limit(c) for c in ch]
        ch = [l[0] for l in lim]
        path = os.path.join(out, h["id"] + ".wav")
        # 两级归一：先写一版避免 int 溢出的临时文件，量出频谱峰值，
        # 再按「频谱峰值 = 0.85」重写 —— 关键：这个增益必须真的写进 WAV，
        # 否则 convolver 实际拿到的电平会比模型（σmax 校验用的缓存）大几十 dB。
        pk = max(max(abs(v) for v in ch[0]), max(abs(v) for v in ch[1]))
        write_wav(path, ch, 0.89 / pk)
        probe = dsp.compute_ir_cache(path, rt60=h["rt60"], pre_ms=h["pre_ms"])
        g = probe["norm_gain"]
        write_wav(path, ch, g * 0.89 / pk)
        d = dsp.compute_ir_cache(path, rt60=h["rt60"], pre_ms=h["pre_ms"])
        assert d["peak_before"] <= dsp.IR_NORM + 1e-3, \
            f"{h['id']} 频谱峰值超限: {d['peak_before']}"
        peak, mid_db = d["peak_before"], d["mid_1k_db"]
        print(f"     限幅: 中位 {lim[0][1]:.3f} 阈值 {lim[0][2]:.3f} "
              f"压掉 {lim[0][3] + lim[1][3]} 个频点；落盘频谱峰值 {peak:.4f}")
        print(f"{h['id']:<16}{h['dur'] + h['pre_ms'] / 1000.0:>7.2f}s{peak:>17.2f}"
              f"{g:>10.4f}{mid_db:>9.1f}dB{h['rt60']:>7.2f}s")
    print("\n全部按「全 FFT bin 频谱峰值 = 0.85」归一化（24bit WAV，留 1.4dB 余量给采样误差）→ |H(f)| ≤ 1")


if __name__ == "__main__":
    main()
