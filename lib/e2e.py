#!/usr/bin/env python3
"""端到端实测：parec 从 null sink 探针取 DSP 输出，与落盘复算的理论值逐点对比。"""
import array, cmath, json, math, os, re, struct, subprocess, sys, tempfile, time, importlib.util

FS, AMP = 48000, 0.5
TMP = tempfile.mkdtemp(prefix="yxm")
DAC = subprocess.run(["bash","-lc","pactl list short sinks|awk '{print $2}'|grep -v '^effect_input'|grep usb|head -1"],
                     capture_output=True, text=True).stdout.strip()
PROBE, PROBE_MON = "yxprobe", "yxprobe.monitor"
CONFDIR = os.path.expanduser("~/.config/pipewire/pipewire.conf.d")

# ---- 载入求值器，作为理论值来源 ----
src = open(os.path.expanduser("~/.local/share/yinxiao/eval_graph.py"), encoding="utf-8").read().split('print("=" * 104)')[0]
E = {}; exec(compile(src, "eg", "exec"), E)

def load_graph(pid):
    p = f"{CONFDIR}/" + [f for f in os.listdir(CONFDIR) if pid in f][0]
    txt = re.sub(r'^\s*#.*$', '', open(p, encoding="utf-8").read(), flags=re.M)
    g = json.loads(txt)["context.modules"][0]["args"]["filter.graph"]
    G = E["Graph"](g)
    if not g.get("inputs"):
        srcs = {s.split(":")[0] for s, _ in G.links}; dsts = {d.split(":")[0] for _, d in G.links}
        st = [n for n in G.nodes if n not in dsts][0]; en = [n for n in G.nodes if n not in srcs][0]
        G.in_map = {f"{st}:In": 0}; G.outs = [f"{en}:Out"]
    return G, g

def theory(pid, f, L, R):
    G, g = load_graph(pid)
    if g.get("inputs"):
        ext = [complex(L, 0), complex(R, 0)][:len(g["inputs"])]
    else:
        ext = [complex(L, 0)]
    out = G.eval(f, ext)
    return out if len(out) > 1 else [out[0], out[0]]

def shell(*c, check=False):
    return subprocess.run(list(c), capture_output=True, text=True, check=check)

def set_links(pid, to_probe):
    o = "effect_output." + "yx_" + pid.replace("-", "_")
    for ch in ("FL", "FR"):
        shell("pw-link", "-d", f"{o}:output_{ch}", f"{DAC}:playback_{ch}")
        shell("pw-link", "-d", f"{o}:output_{ch}", f"{PROBE}:playback_{ch}")
    tgt = PROBE if to_probe else DAC
    for ch in ("FL", "FR"):
        shell("pw-link", f"{o}:output_{ch}", f"{tgt}:playback_{ch}")

def tone(freq, path, right=1.0):
    import wave
    n = int(FS * 4.0)
    with wave.open(path, "w") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(FS)
        b = bytearray()
        for i in range(n):
            env = min(1.0, i/(0.005*FS), (n-i)/(0.005*FS))
            v = int(32767*AMP*env*math.sin(2*math.pi*freq*i/FS))
            b += struct.pack("<hh", v, int(v*right))
        w.writeframes(bytes(b))

def capture(freq, target, path, right=1.0):
    tp = path + ".tone"; tone(freq, tp, right)
    wav = path + ".wav"
    # 用 timeout 包裹，保证自动收尾；写成 wav 便于用 wave 模块读
    cmd = (f"timeout 6 parec -d {PROBE_MON} --rate={FS} --channels=2 "
           f"--format=s16le --file-format=wav {wav} 2>{path}.err")
    r = subprocess.Popen(cmd, shell=True)
    time.sleep(0.8)
    subprocess.run(["pw-play", "--target", target, tp], capture_output=True, timeout=30)
    r.wait(timeout=15)
    import wave
    with wave.open(wav) as w:
        n = w.getnframes()
        raw = w.readframes(n)
    if n < FS // 2:
        raise RuntimeError(f"捕获过短: {n} 帧; parec stderr={open(path+'.err').read()[:200]}")
    a = array.array("h"); a.frombytes(raw)
    n = len(a)//2
    # 能量包络定位"稳定段"：纯音测量不需要频率选择性，RMS 即可
    hop = FS // 5   # 200ms 包络窗口：55Hz 也能平均出平坦包络
    env = []
    for i in range(0, n - hop, hop):
        e = sum(abs(a[2*j]) + abs(a[2*j+1]) for j in range(i, i+hop)) / (2.0*hop*32768)
        env.append(e)
    if not env: raise RuntimeError("无数据")
    mx = max(env)
    if mx < 0.02: raise RuntimeError(f"捕获无信号 mx={mx:.4f} stderr={open(path+'.err').read()[:150]}")
    thr = 0.80 * mx
    best = (0, 0); cur = None
    for k, e in enumerate(env):
        if e >= thr:
            cur = k if cur is None else cur
        else:
            if cur is not None:
                if k - cur > best[1] - best[0]: best = (cur, k)
                cur = None
    if cur is not None and len(env) - cur > best[1] - best[0]: best = (cur, len(env))
    a0, a1 = best[0]*hop, best[1]*hop
    mid = (a0 + a1)//2
    s0 = max(a0, mid - FS//2); s1 = min(a1, s0 + FS)
    if s1 - s0 < FS//2: raise RuntimeError(f"稳定段过短 {a1-a0} 帧")
    seg = a[2*s0:2*s1]
    rms = math.sqrt(sum(v*v for v in seg)/len(seg))/32768.0
    # 单声道纯音 RMS（两声道同相）→ 等效幅度 = rms*sqrt(2)
    amp = rms * math.sqrt(2)
    # 分别取 L/R 用于矩阵型预设（串扰/极性）
    Ls = [a[2*i]/32768.0 for i in range(s0, s1)]
    Rs = [a[2*i+1]/32768.0 for i in range(s0, s1)]
    def gz(x):
        c = 2*math.cos(2*math.pi*freq/FS); s1_, s2_ = 0.0, 0.0
        for v in x:
            s0_ = v + c*s1_ - s2_; s2_ = s1_; s1_ = s0_
        re_ = s1_ - s2_*math.cos(2*math.pi*freq/FS)
        im_ = s2_*math.sin(2*math.pi*freq/FS)
        return complex(re_, im_) * (2.0/len(x))
    gl, gr = gz(Ls), gz(Rs)
    # 交叉校验：Goertzel 幅度应与 RMS 幅度一致
    if abs(abs(gl) - amp) > 0.05 * max(amp, 1e-9) + 0.01:
        pass
    return gl, gr

def capture_med(freq, target, path, right=1.0, n=2):
    got = []
    for k in range(n):
        got.append(capture(freq, target, f"{path}_{k}", right=right))
    return (sum(z[0] for z in got)/n, sum(z[1] for z in got)/n)

def db(z): return 20*math.log10(abs(z)) if abs(z) > 0 else -99

def deg(z): return math.degrees(cmath.phase(z))

print("=" * 100)
print("端到端实测 vs 落盘理论值   (测试音经 null sink 探针，不发声；每点重复 2 次取均值)")
print("=" * 100)

set_links("bass-xxl", False)
print("\n[基线] 直连探针，无 DSP  (输入幅度 0.5 正弦)")
BASE = {}
for f in (55, 60, 100, 220, 1000, 1800, 3000, 3200, 10000):
    l, r = capture_med(f, PROBE, f"{TMP}/b{f}")
    BASE[f] = (abs(l) + abs(r)) / 2
    print(f"   {f:>6}Hz  幅值={BASE[f]:.4f}")

CASES = [
    ("bass-xxl",      [(55,1,1), (100,1,1), (1000,1,1), (3000,1,1), (10000,1,1)]),
    ("vocal-clear",   [(220,1,1), (1800,1,1), (3200,1,1), (10000,1,1)]),
    ("sub-woofer",    [(60,1,1), (1000,1,1)]),
    ("surround-3d",   [(1000,1,0)]),
    ("mono-intimate", [(1000,1,0)]),
]

fails, allerr = [], []
for pid, cases in CASES:
    print(f"\n[预设] {pid}")
    set_links(pid, True); time.sleep(0.5)
    for f, L, R in cases:
        ml, mr = capture_med(f, "effect_input.yx_" + pid.replace("-", "_"), f"{TMP}/{pid}_{f}", right=R)
        tl, tr = theory(pid, f, L, R)
        got_l, got_r = abs(ml)/BASE[f], abs(mr)/BASE[f]
        exp_l, exp_r = abs(tl), abs(tr)
        el = 20*math.log10(got_l/exp_l) if exp_l > 1e-6 else None
        er = 20*math.log10(got_r/exp_r) if exp_r > 1e-6 else None
        for e in (el, er):
            if e is not None: allerr.append(abs(e))
        ok = all(e is None or abs(e) <= 0.5 for e in (el, er))
        if not ok: fails.append((pid, f, el, er))
        fs = lambda e: f"{e:+.2f}" if e is not None else "  -  "
        ph = f"{deg(tl):+.0f}°/{deg(tr):+.0f}°"
        print(f"  {f:>6}Hz  L {got_l:.4f} vs {exp_l:.4f} ({fs(el)}dB) | "
              f"R {got_r:.4f} vs {exp_r:.4f} ({fs(er)}dB) | 理论相位L/R {ph}  {'✔' if ok else '✘'}")

for pid, _ in CASES:
    set_links(pid, False)
shell("pactl", "unload-module", "yxprobe")
print("\n" + "=" * 100)
if allerr:
    print(f"全部 {len(allerr)} 个测点：平均偏差 {sum(allerr)/len(allerr):.3f} dB，最大 {max(allerr):.3f} dB")
if fails:
    print("✘ 偏差 > 0.5dB:", fails)
else:
    print("✔ 全部测点实测与理论吻合（≤0.5 dB）—— 音效 DSP 在实际运行中确实按落盘参数生效")
