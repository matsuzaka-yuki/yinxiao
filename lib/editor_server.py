#!/usr/bin/env python3
"""yinxiao 可视化编辑器 —— 本地 HTTP 服务（仅监听 127.0.0.1）。

DSP 数学复用 dsp.py（与已实测验证的 eval_graph.py 同源），
前端只负责画曲线和交互。支持：
  · 实时试听：拖动时把 Freq/Q/Gain 直接推给运行中的 filter-chain，无需重启
  · 保存：写回 conf 文件（自动备份 .bak），自动重算 preamp 保证不削顶
"""
import json, os, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dsp

HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "editor.html")
PORT = int(os.environ.get("YINXIAO_PORT", "8787"))

COEF = {"b0", "b1", "b2", "a0", "a1", "a2"}
_LOCK = threading.Lock()
SESS = {}          # preset_id -> {"args":..., "orig_spec":..., "spec":...}


def _clean_controls(args):
    """只下发语义参数（Freq/Q/Gain/mixer 增益），不下发算出来的系数 b0..a2。"""
    out = {}
    for n in args["filter.graph"]["nodes"]:
        for k, v in (n.get("control") or {}).items():
            if k.split(":")[-1] in COEF:
                continue
            if isinstance(v, (int, float)):
                out[f"{n['name']}:{k}"] = float(v)
    return out


def session(pid):
    if pid in SESS:
        return SESS[pid]
    path = f"{dsp.CONFDIR}/" + [os.path.basename(p) for p in dsp.conf_files()
                                if dsp.preset_id_of(p) == pid][0]
    args = dsp.load_graph(path)
    spec = dsp.describe(args)
    SESS[pid] = {"path": path, "args": args, "orig_spec": json.loads(json.dumps(spec)),
                 "spec": spec}
    return SESS[pid]


def auto_preamp(args, spec):
    """按当前 band 设置重算 preamp，使整条链峰值 = 0 dB（永不削顶）。"""
    probe = json.loads(json.dumps(args))
    s2 = json.loads(json.dumps(spec))
    s2["preamp"] = 0.0
    dsp.apply_describe(probe, s2)
    # 混响型必须用解析上界：IR 是噪声型信号，曲线上的 σmax 会低估真实峰值
    b = dsp.reverb_bound(probe["filter.graph"])
    if b is not None:
        return -math_ceil_quarter(b), b
    peak_db = max(dsp.analyze(probe["filter.graph"])["sigma_db"])
    return -math_ceil_quarter(peak_db), peak_db


def math_ceil_quarter(x, margin=0.25):
    import math
    return math.ceil((x + margin) * 4) / 4.0


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/favicon.ico":
            self._send(204, b"", "image/x-icon"); return
        if p in ("/", "/index.html"):
            try:
                self._send(200, open(HTML, "rb").read(), "text/html; charset=utf-8")
            except OSError as e:
                self._send(500, {"error": str(e)})
            return
        if p == "/api/state":
            with _LOCK:
                presets = dsp.list_presets()
                for d in presets:
                    s = session(d["id"])
                    d["dirty"] = s["spec"] != s["orig_spec"]
                    d["bands"] = s["spec"]["bands"]
                    d["preamp"] = s["spec"]["preamp"]
                    d["kind"] = s["spec"]["kind"]
                    d["matrix"] = s["spec"]["matrix"]
                    d["reverb"] = s["spec"]["reverb"]
                self._send(200, {"presets": presets, "active_sink": dsp.active_sink(),
                                 "prefix": dsp.PREFIX, "nodes": dsp.node_ids()})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"error": f"bad json: {e}"}); return
        act = self.path.split("?")[0]
        pid = req.get("preset_id", "")
        try:
            with _LOCK:
                s = session(pid)
                if act in ("/api/add_band", "/api/del_band"):
                    if act == "/api/add_band":
                        nm, err = dsp.insert_band(s["args"])
                    else:
                        nm, err = dsp.remove_band(s["args"], req.get("node", ""))
                    if err and "不支持" in err or (err and "不能删除" in err):
                        r = self._analysis(s); r["error"] = err; self._send(200, r); return
                    dsp.apply_describe(s["args"], s["spec"])   # 保持已改的参数
                    disk = dsp.describe(s["args"])
                    s["spec"]["bands"] = disk["bands"]
                    r = self._analysis(s); r["dirty"] = True
                    self._send(200, r); return
                if act == "/api/select":
                    target = dsp.PREFIX + pid.replace("-", "_")
                    import subprocess
                    subprocess.run(["pactl", "set-default-sink", target],
                                   capture_output=True, timeout=5)
                    # 只搬"应用"的流；绝不能搬 effect_output.* 预设输出流，
                    # 否则会破坏 node.target 钉定 → 音频被路由到别的设备。
                    for si in dsp.app_sink_inputs():
                        subprocess.run(["pactl", "move-sink-input", si, target],
                                       capture_output=True, timeout=5)
                    r = self._analysis(s)
                    r["active_sink"] = dsp.active_sink()
                    r["preamp"] = s["spec"]["preamp"]
                    self._send(200, r); return
                if act == "/api/off":
                    import subprocess
                    hw = ""
                    try:
                        hw = open(os.path.expanduser("~/.local/share/yinxiao/hw-sink")).read().strip()
                    except OSError:
                        pass
                    if hw:
                        subprocess.run(["pactl", "set-default-sink", hw], capture_output=True, timeout=5)
                        for si in dsp.app_sink_inputs():
                            subprocess.run(["pactl", "move-sink-input", si, hw],
                                           capture_output=True, timeout=5)
                    r = self._analysis(s)
                    r["active_sink"] = dsp.active_sink()
                    self._send(200, r); return
                if act == "/api/restore_bak":
                    import shutil
                    bak = s["path"] + ".bak"
                    if os.path.exists(bak):
                        shutil.copy2(bak, s["path"])
                        disk = dsp.load_graph(s["path"])
                        s["args"] = disk
                        s["spec"] = dsp.describe(disk)
                        s["orig_spec"] = json.loads(json.dumps(s["spec"]))
                        r = self._analysis(s)
                        r["restored"] = True
                        r["preamp"] = s["spec"]["preamp"]
                        if req.get("live"):
                            nid = dsp.node_ids().get(dsp.PREFIX + pid.replace("-", "_"))
                            if nid: dsp.live_set(nid, _clean_controls(s["args"]))
                    else:
                        r = self._analysis(s); r["error"] = "没有 .bak 可回滚"
                    self._send(200, r); return
                if act == "/api/revert":
                    disk = dsp.load_graph(s["path"])
                    s["args"] = disk
                    s["spec"] = dsp.describe(disk)
                    s["orig_spec"] = json.loads(json.dumps(s["spec"]))
                    r = self._analysis(s)
                    self._send(200, r); return

                spec = req.get("spec")
                if isinstance(spec, dict):
                    s["spec"].update({k: v for k, v in spec.items() if k in
                                      ("bands", "matrix", "preamp", "reverb")})

                # 自动 preamp（除非前端显式关了）
                peak_db = None
                if s["spec"]["kind"] in ("eq", "reverb") and req.get("auto_preamp", True):
                    p, peak_db = auto_preamp(s["args"], s["spec"])
                    s["spec"]["preamp"] = p

                dsp.apply_describe(s["args"], s["spec"])
                r = self._analysis(s)
                r["peak_db"] = peak_db
                r["dirty"] = s["spec"] != s["orig_spec"]
                r["preamp"] = s["spec"]["preamp"]

                if req.get("live"):
                    nid = dsp.node_ids().get(dsp.PREFIX + pid.replace("-", "_"))
                    if nid is None:
                        r["live_error"] = "找不到运行中的节点（预设未加载？）"
                    else:
                        ok, err = dsp.live_set(nid, _clean_controls(s["args"]))
                        r["live"] = ok
                        if not ok: r["live_error"] = err

                if req.get("save"):
                    dsp.save_graph(s["path"], s["args"],
                                   note=f"yinxiao preset: {pid} (edited by editor)")
                    s["orig_spec"] = json.loads(json.dumps(s["spec"]))
                    r["saved"] = True
                    r["dirty"] = False
                self._send(200, r)
        except Exception as e:
            import traceback
            self._send(500, {"error": str(e), "trace": traceback.format_exc()[-800:]})

    def _analysis(self, s):
        graph = s["args"]["filter.graph"]
        a = dsp.analyze(graph)
        extra = {}
        if s["spec"]["kind"] == "reverb":
            extra["sigma_bound"] = dsp.reverb_bound(graph)      # 严格上界（dB）
            extra["mix_1k_db"] = dsp.reverb_mix_db(graph)       # 1kHz 湿/干
            extra["reverb"] = s["spec"]["reverb"]
            # 噪声型 IR 的原始曲线太尖；平滑曲线才是"听感走向"
            extra["ch_smooth"] = [round(v, 3) for v in
                                  dsp.smooth_third_octave(a["ch_db"], a["freqs"])]
            extra["sigma_smooth"] = [round(v, 3) for v in
                                     dsp.smooth_third_octave(a["sigma_db"], a["freqs"])]
        out = {
            "freqs": [round(f, 2) for f in a["freqs"]],
            "sigma_db": [round(v, 3) for v in a["sigma_db"]],
            "ch_db": [round(v, 3) for v in a["ch_db"]],
            "mono_db": [round(v, 3) for v in a["mono_db"]],
            "corr_db": [round(v, 3) for v in a["corr_db"]],
            "side_db": [round(v, 3) for v in a["side_db"]],
            "sigma_max": round(a["sigma_max"], 5)}
        out.update({k: (round(v, 3) if isinstance(v, float) else v)
                    for k, v in extra.items()})
        return {"analysis": out}


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "editor.pid"), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    print(f"yinxiao 编辑器: http://127.0.0.1:{PORT}")
    if "--open" in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
