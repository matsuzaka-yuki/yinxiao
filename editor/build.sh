#!/usr/bin/env bash
# 开发期构建：Tailwind 编译 → 内联 CSS 与 Preact/htm → 单文件 ../lib/editor.html
# 只有改前端时才需要跑（需要 node）。用户安装不需要 node：lib/editor.html 已提交。
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d node_modules ]; then
    echo "▶ 安装构建依赖（仅开发期）"
    npm install --no-audit --no-fund
fi
mkdir -p .build
echo "▶ 编译 Tailwind（只保留源码里用到的类）"
npx tailwindcss -i editor.tailwind.css -o .build/tw.css --minify
echo "▶ 内联装配"
python3 build.py ../lib/editor.html
