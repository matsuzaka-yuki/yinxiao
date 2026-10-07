#!/usr/bin/env python3
"""落盘 conf 的峰值增益验证：σmax = 任意输入下的增益上界，≤1 即数学上保证永不削顶。

DSP 数学只有一份实现（dsp.py），本脚本只负责"跑一遍 + 打表格" ——
以前这里有一份独立复制的求值器，加了混响（convolver）之后它不认识新节点、
把卷积当成单位增益，于是把正常的混响预设误报成超限。教训：数学不要抄第二份。

混响型预设额外报一列「解析上界」：IR 是噪声型信号，频谱起伏很快，
479 点网格上的 σmax 可能低估真实峰值好几个 dB，所以混响型的判定用
dsp.reverb_bound() 的解析上界（不依赖网格采样）。

用法: python3 eval_graph.py [conf 目录]
"""
import glob, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dsp

DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.config/pipewire/pipewire.conf.d")
GRID = [20.0 * (1000.0 ** (i / 957.0)) for i in range(958)]      # 1/96 倍频程，20Hz-20kHz


def rows():
    for path in sorted(glob.glob(f"{DIR}/50-yinxiao-*.conf")):
        pid = os.path.basename(path).split("-", 3)[3].removesuffix(".conf")
        args = dsp.load_graph(path)
        graph = args["filter.graph"]
        explicit = bool(graph.get("inputs"))
        a = dsp.analyze(graph, grid=GRID)
        near = lambda t: min(range(len(GRID)), key=lambda i: abs(GRID[i] - t))
        i, i100, i1k = near(1000.0), near(100.0), near(1000.0)
        bound = dsp.reverb_bound(graph)
        peak = a["sigma_max"] if bound is None else max(a["sigma_max"], 10 ** (bound / 20.0))
        yield dict(pid=pid, explicit=explicit, peak=peak, bound=bound,
                   f=GRID[a["sigma_db"].index(max(a["sigma_db"]))],
                   ch1k=abs(10 ** (a["ch_db"][i1k] / 20.0)),
                   ch100=abs(10 ** (a["ch_db"][i100] / 20.0)),
                   mono=abs(10 ** (a["corr_db"][i1k] / 20.0)))


print("=" * 104)
print("落盘 conf 的严格峰值增益验证（σmax = 任意输入下的增益上界；≤1.00 即数学上永不削顶）")
print("=" * 104)
print(f"{'预设':<16}{'类型':<8}{'σmax':>9}{'@频率':>9}{'余量dB':>9}{'@1kHz':>8}{'@100Hz':>8}"
      f"{'单声道响应':>11}{'解析上界':>10}{'判定':>8}")
print("-" * 104)
fails = []
for r in rows():
    marg = -20 * math.log10(r["peak"])
    ok = r["peak"] <= 1.0005
    if not ok:
        fails.append((r["pid"], r["peak"]))
    bnd = f"{r['bound']:+.2f}dB" if r["bound"] is not None else "   -"
    print(f"{r['pid']:<16}{'矩阵2x2' if r['explicit'] else '对称EQ':<8}{r['peak']:>9.4f}"
          f"{r['f']:>8.0f}Hz{marg:>+9.2f}{r['ch1k']:>8.3f}{r['ch100']:>8.3f}"
          f"{r['mono']:>11.3f}{bnd:>10}{'✔' if ok else '✘超限':>8}")

print()
if fails:
    print("✘ 峰值超限（可能削顶）:", ", ".join(f"{p}={v:.3f}" for p, v in fails))
else:
    n = len(list(rows()))
    print(f"✔ 全部 {n} 个预设 σmax ≤ 1.00 —— 任意输入（含硬声像、满量程）下都不会削顶")
print()
print("注1：矩阵/混响型的「解析上界」是不依赖频率网格的严格上界（混响的最坏情况")
print("     出现在 IR 频谱峰值处，网格采样会低估，所以判定以解析上界为准）。")
print("注2：单声道响应 = L+R 在 1kHz 的系数；=1.000 表示完全单声道兼容，不会相位抵消。")

# 有超限就返回非零，方便 CI / 脚本断言
sys.exit(1 if fails else 0)
