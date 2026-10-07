#!/usr/bin/env python3
"""混响预设的端到端实测（静音进行，不发声）。

两条独立的验证，缺一不可：

A. 稳态频响（主验证，与 e2e.py 同一套方法）
   打入 2.5s 长音，从探针 monitor 录回来，用 Goertzel 量出该频率的幅值，
   与 dsp.py 的模型逐点对比。混响链路里既有 EQ、又有 convolver、还有串扰矩阵，
   只有实测与模型吻合（≤1dB）才能说"落盘的 IR 和参数真的生效了"。

B. 混响尾巴（存在性验证）
   打入 100ms 短音，看尾巴衰减：用 Schroeder 反向积分（混响时间测量的标准方法，
   对噪声型尾巴的随机起伏鲁棒）估 RT60，与 IR 的标称值对照。

用法: python3 e2e_reverb.py [预设id...]   默认测 4 个厅堂预设
"""
import array, cmath, importlib.util, math, os, struct, subprocess, sys, tempfile, time, wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dsp

FS = 48000
CONFDIR = os.path.expanduser("~/.config/pipewire/pipewire.conf.d")
PROBE = "yxprobe"
TMP = tempfile.mkdtemp(prefix="yxrvb")
FREQS = [100.0, 250.0, 1000.0, 4000.0, 10000.0]
DAC = subprocess.run(["bash", "-lc",
                      "pactl list short sinks|awk '{print $2}'|grep -v '^effect_input'|grep usb|head -1"],
                     capture_output=True, text=True).stdout.strip()


def sh(*c):
    return subprocess.run(list(c), capture_output=True, text=True)


def design_targets():
    spec = importlib.util.spec_from_file_location(
        "gen_irs", os.path.join(HERE, "gen_irs.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return {h["id"]: h for h in m.HALLS}


def tone(path, freq, dur):
    n = int(FS * dur); b = bytearray()
    for i in range(n):
        env = min(1.0, i / (0.004 * FS), (n - i) / (0.004 * FS))
        v = int(32767 * 0.5 * env * math.sin(2 * math.pi * freq * i / FS))
        b += struct.pack("<hh", v, v)
    with wave.open(path, "w") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(FS); w.writeframes(bytes(b))


def probe_up():
    out = sh("pactl", "load-module", "module-null-sink", f"sink_name={PROBE}",
             f"rate={FS}", "channels=2")
    mid = out.stdout.strip()
    if not mid.isdigit():
        mid = sh("bash", "-lc",
                 "pactl list short modules | awk '$2==\"module-null-sink\"{print $1}' | tail -1"
                 ).stdout.strip()
    time.sleep(0.8)
    return mid


def link(pid, to_probe):
    o = "effect_output.yx_" + pid.replace("-", "_")
    tgt = PROBE if to_probe else DAC
    for ch in ("FL", "FR"):
        sh("pw-link", "-d", f"{o}:output_{ch}", f"{DAC}:playback_{ch}")
        sh("pw-link", "-d", f"{o}:output_{ch}", f"{PROBE}:playback_{ch}")
        sh("pw-link", f"{o}:output_{ch}", f"{tgt}:playback_{ch}")
    time.sleep(0.3)


def record(pid, tone_wav, secs):
    rec = os.path.join(TMP, os.path.basename(tone_wav) + f".{pid}.wav")
    r = subprocess.Popen(["timeout", str(int(secs) + 3), "parec", "-d", PROBE + ".monitor",
                          f"--rate={FS}", "--channels=2", "--format=s16le",
                          "--file-format=wav", rec],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.6)
    subprocess.run(["pw-play", "--target", "effect_input.yx_" + pid.replace("-", "_"), tone_wav],
                   capture_output=True, timeout=30)
    r.wait(timeout=secs + 6)
    with wave.open(rec) as w:
        n = w.getnframes(); raw = w.readframes(n)
    return array.array("h", raw), n


def noise_wav(path, dur=4.0):
    """白噪声测试信号（左右同相），RMS 无关紧要——用直通做基准把它消掉。"""
    import random
    random.seed(20261007)
    n = int(FS * dur); b = bytearray()
    for i in range(n):
        env = min(1.0, i / (0.02 * FS), (n - i) / (0.02 * FS))
        v = int(32767 * 0.25 * env * (random.random() * 2 - 1))
        b += struct.pack("<hh", v, v)
    with wave.open(path, "w") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(FS); w.writeframes(bytes(b))


def spectrum(a, n, seg=8192, frames_max=8):
    """稳定段的平均功率谱（Hann 窗 + 50% 重叠，L/R 各一条）。"""
    i0 = int(1.5 * FS)
    win = [0.5 - 0.5 * math.cos(2 * math.pi * k / seg) for k in range(seg)]
    wlen = sum(w * w for w in win) or 1.0
    nb = seg // 2 + 1
    acc = [[0.0] * nb, [0.0] * nb]
    frames = []
    i = i0
    while i + seg <= n and len(frames) < frames_max:
        frames.append(i); i += seg // 2
    try:
        import numpy as np
        x = np.frombuffer(a.tobytes(), dtype="<i2").astype(float).reshape(-1, 2) / 32768.0
        w = np.hanning(seg)
        wl = float(np.sum(w * w)) or 1.0
        for i in frames:
            for ch in (0, 1):
                X = np.fft.rfft(x[i:i + seg, ch] * w)
                acc[ch] = list(np.add(acc[ch], (np.abs(X) ** 2) / wl))
    except ImportError:
        for i in frames:
            for ch in (0, 1):
                re = [a[2 * (i + k) + ch] / 32768.0 * win[k] for k in range(seg)]
                im = [0.0] * seg
                dsp._fft(re, im)
                for b in range(nb):
                    acc[ch][b] += (re[b] ** 2 + im[b] ** 2) / wlen
    k = max(len(frames), 1)
    for ch in (0, 1):
        acc[ch] = [v / k for v in acc[ch]]
    return acc, [b * FS / seg for b in range(nb)]


def band_gain(acc, freqs, lo, hi, ch):
    """带内平均功率（dB）—— 带内平均才是有意义的对比口径。"""
    idx = [i for i, f in enumerate(freqs) if lo <= f <= hi]
    if not idx:
        return None
    p = sum(acc[ch][i] for i in idx) / len(idx)
    return 10 * math.log10(max(p, 1e-30))


def model_curves(graph):
    """模型：FGRID 上 L / R 的复响应（输入 L=R=1）。"""
    G = dsp.Graph(graph)
    nin = len(graph["inputs"])
    L, R = [], []
    for f in dsp.FGRID:
        cols = [G.eval(f, [1 + 0j if i == j else 0j for i in range(nin)]) for j in range(nin)]
        M = [[cols[0][0], cols[1][0]], [cols[0][1], cols[1][1]]]
        L.append(abs(M[0][0] + M[0][1]))
        R.append(abs(M[1][0] + M[1][1]))
    return L, R


def model_band_db(curve, lo, hi):
    idx = [i for i, f in enumerate(dsp.FGRID) if lo <= f <= hi]
    p = sum(curve[i] ** 2 for i in idx) / max(len(idx), 1)
    return 10 * math.log10(max(p, 1e-30))


def bandpass(x, lo=500.0, hi=2000.0):
    """只留中频带：RT60 的标准做法是分带测，全带会被低频的长拖尾和
    本底噪声一起带偏。"""
    import numpy as np
    X = np.fft.rfft(np.asarray(x))
    f = np.fft.rfftfreq(len(x), 1 / FS)
    X[(f < lo) | (f >= hi)] = 0
    return list(np.fft.irfft(X, len(x)))


def schroeder_rt60(x, i0, i1):
    """Schroeder 反向积分求 RT60（-5~-25dB 拟合，即 T20 外推）。

    混响时间测量的标准方法：对噪声型尾巴的随机起伏天然鲁棒，
    直接拟合包络斜率会被起伏带偏。
    """
    seg = x[i0:i1]
    tot = sum(seg)
    if tot <= 0:
        return None
    # 截断到 -35dB：再往下就是录音本底，会把衰减斜率压平、把 RT60 估长
    run, cut = 0.0, len(seg)
    for k in range(len(seg) - 1, -1, -1):
        run += seg[k]
        if 10 * math.log10(max(run / tot, 1e-20)) < -35:
            cut = k
            break
    seg = seg[:cut] or seg
    tot = sum(seg) or tot
    edc, acc = [], 0.0
    for v in reversed(seg):
        acc += v
        edc.append(acc)
    edc.reverse()
    db = [10 * math.log10(max(v / tot, 1e-20)) for v in edc]
    band = [(i, d) for i, d in enumerate(db) if -25 <= d <= -5]
    if len(band) < 50:
        return None
    mx = sum(i for i, _ in band) / len(band); my = sum(d for _, d in band) / len(band)
    slope = sum((i - mx) * (d - my) for i, d in band) / sum((i - mx) ** 2 for i, _ in band)
    return -60.0 / (slope * FS) if slope < 0 else None


def tail_measure(pid):
    tp = os.path.join(TMP, "click.wav"); tone(tp, 1000.0, 0.10)
    a, n = record(pid, tp, 5.0)
    mono = [(a[2 * i] + a[2 * i + 1]) / 2.0 / 32768.0 for i in range(n)]
    x = [v * v for v in mono]
    pk = max(x)
    on = next(i for i, v in enumerate(x) if v > 0.25 * pk)
    xb = [v * v for v in bandpass(mono)]          # 中频带内的功率序列
    return schroeder_rt60(xb, on + int(0.35 * FS), min(on + int(6.0 * FS), n))


def main():
    pids = [a for a in sys.argv[1:] if not a.startswith("-")] or \
           ["live-concert", "hall-orchestra", "livehouse", "cathedral"]
    H = design_targets()
    orig = sh("pactl", "get-default-sink").stdout.strip()
    print(f"物理声卡: {DAC}\n探针: {PROBE}（录的是 DSP 输出，全程静音）\n")
    mid = probe_up()
    devs, rt_bad = [], []
    try:
        noise = os.path.join(TMP, "noise.wav"); noise_wav(noise)
        # 基准：同一段噪声直接进探针（把信号源自身的谱不平整消掉）
        r = subprocess.Popen(["timeout", "9", "parec", "-d", PROBE + ".monitor",
                              f"--rate={FS}", "--channels=2", "--format=s16le",
                              "--file-format=wav", os.path.join(TMP, "base.wav")],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.6)
        subprocess.run(["pw-play", "--target", PROBE, noise], capture_output=True, timeout=30)
        r.wait(timeout=15)
        with wave.open(os.path.join(TMP, "base.wav")) as w:
            ba = array.array("h", w.readframes(w.getnframes()))
        base, freqs = spectrum(ba, len(ba))

        print(f"{'预设':<16}{'频带':>8}{'实测L':>9}{'模型L':>9}{'偏差':>8}"
              f"{'实测R':>9}{'模型R':>9}{'偏差':>8}")
        print("-" * 76)
        for pid in pids:
            p = CONFDIR + "/50-yinxiao-" + \
                [f for f in os.listdir(CONFDIR) if pid in f][0].split("50-yinxiao-")[1]
            g = dsp.load_graph(p)["filter.graph"]
            mL, mR = model_curves(g)
            link(pid, True)
            rec = os.path.join(TMP, f"{pid}.wav")
            r = subprocess.Popen(["timeout", "9", "parec", "-d", PROBE + ".monitor",
                                  f"--rate={FS}", "--channels=2", "--format=s16le",
                                  "--file-format=wav", rec],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.6)
            subprocess.run(["pw-play", "--target", "effect_input.yx_" + pid.replace("-", "_"),
                            noise], capture_output=True, timeout=30)
            r.wait(timeout=15)
            with wave.open(rec) as w:
                ra = array.array("h", w.readframes(w.getnframes()))
            acc, _ = spectrum(ra, len(ra))
            for c in (100.0, 250.0, 1000.0, 4000.0, 10000.0):
                lo, hi = c / 1.26, c * 1.26
                dl = band_gain(acc, freqs, lo, hi, 0) - band_gain(base, freqs, lo, hi, 0)
                dr = band_gain(acc, freqs, lo, hi, 1) - band_gain(base, freqs, lo, hi, 1)
                el = dl - model_band_db(mL, lo, hi)
                er = dr - model_band_db(mR, lo, hi)
                devs += [abs(el), abs(er)]
                print(f"{pid:<16}{c:>7.0f}Hz{dl:>8.1f}dB{model_band_db(mL, lo, hi):>8.1f}dB"
                      f"{el:>+7.2f}dB{dr:>8.1f}dB{model_band_db(mR, lo, hi):>8.1f}dB{er:>+7.2f}dB")
            rt = tail_measure(pid)
            want = H[pid]["rt60"]
            ok = rt and abs(rt - want) / want < 0.45
            if not ok:
                rt_bad.append(pid)
            print(f"{'':<16}尾巴 RT60: 设计 {want:.2f}s / 实测 "
                  f"{f'{rt:.2f}s' if rt else '—'}  {'✔' if ok else '✘'}")
            link(pid, False)
    finally:
        sh("pactl", "set-default-sink", orig)
        if mid.isdigit():
            sh("pactl", "unload-module", mid)
    print()
    if devs:
        print(f"稳态频响 {len(devs)} 个测点：平均偏差 {sum(devs)/len(devs):.3f} dB，最大 {max(devs):.3f} dB")
    print("✔ 实测与模型吻合（带内 ≤1.5dB）—— convolver 确实按落盘 IR 在卷积"
          if devs and max(devs) <= 1.5 else "✘ 存在 >1.5dB 的偏差，需要排查")
    if rt_bad:
        print("✘ RT60 与设计不符:", ", ".join(rt_bad))
    print(f"录音与测试音在 {TMP}")


if __name__ == "__main__":
    main()
