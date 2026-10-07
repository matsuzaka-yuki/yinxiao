#!/usr/bin/env python3
"""把 Tailwind 产物 + Preact/htm 内联进单文件编辑器。

开发期用（需要 node 跑 Tailwind CLI），产物 lib/editor.html 是纯静态单文件：
运行期不请求任何外部资源，离线可用。
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "editor.src.html")
TW = os.path.join(HERE, ".build", "tw.css")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "lib", "editor.html")

def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()

def strip_map(js):
    return re.sub(r'\n?//# sourceMappingURL=\S+\s*$', '\n', js, flags=re.M)

vendor = "\n".join(strip_map(read(os.path.join(HERE, "vendor", f))) for f in (
    "preact/dist/preact.min.umd.js",
    "preact/hooks/dist/hooks.umd.js",
    "htm/dist/htm.umd.js",
))
tw = read(TW)
html = read(SRC)
assert "/*__TAILWIND__*/" in html and "/*__VENDOR__*/" in html, "源文件缺少占位符"
html = html.replace("/*__TAILWIND__*/", tw).replace("/*__VENDOR__*/", vendor)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"写出 {OUT}  ({len(html)/1024:.1f} KB：源码 + Tailwind {len(tw)/1024:.1f}KB + 内联库 {len(vendor)/1024:.1f}KB)")
