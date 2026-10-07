#!/usr/bin/env bash
# yinxiao —— 一键安装：检测物理声卡 → 生成 conf 与脉冲响应 → 装命令行与 systemd 服务
#
#   bash install.sh                  # 自动检测声卡
#   bash install.sh --sink <sink名>   # 手动指定
#   bash install.sh --dry-run        # 只打印会做什么，不动系统
#   bash install.sh --no-services    # 不装 systemd 服务（只装命令行与预设）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/yinxiao"
BINDIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONFDIR="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d"
PREFIX="effect_input.yx_"

SINK=""; DRY=0; SERVICES=1
while [ $# -gt 0 ]; do
    case "$1" in
        --sink) SINK="${2:-}"; shift 2 ;;
        --sink=*) SINK="${1#*=}"; shift ;;
        --dry-run) DRY=1; shift ;;
        --no-services) SERVICES=0; shift ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[36m▶\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then echo "    [dry-run] $*"; else eval "$@"; fi; }

# ---------------------------------------------------------------- 0. 依赖检查
say "检查依赖"
miss=0
for c in pipewire pactl pw-cli pw-link python3; do
    command -v "$c" >/dev/null 2>&1 || { warn "缺少 $c"; miss=1; }
done
[ "$miss" = 0 ] || { echo "请先装好 PipeWire（含 pulse 兼容层）与 python3"; exit 1; }
python3 - <<'PY' || true
import sys
if sys.version_info < (3, 8):
    print("! python3 需要 3.8+（当前 %s）" % sys.version.split()[0]); sys.exit(1)
try:
    import numpy  # noqa
    print("  numpy 可用：IR 生成会快很多")
except ImportError:
    print("  （无 numpy：IR 生成走纯 Python 路径，约需 1 分钟）")
PY
pw_ver="$(pipewire --version 2>/dev/null | head -1 || true)"
say "PipeWire: ${pw_ver:-未知}"
PLUGIN="$(ls /usr/lib*/spa-0.2/filter-graph/libspa-filter-graph-plugin-builtin.so \
             /usr/lib/*/spa-0.2/filter-graph/libspa-filter-graph-plugin-builtin.so 2>/dev/null | head -1 || true)"
if [ -n "$PLUGIN" ] && ! strings "$PLUGIN" 2>/dev/null | grep -q '^convolver$'; then
    warn "这个 builtin 插件里没有 convolver —— 4 个厅堂混响预设会加载失败，"
    warn "其余 17 个 EQ/矩阵预设不受影响。"
fi

# ---------------------------------------------------------------- 1. 物理声卡
list_physical() {
    pactl list short sinks | awk '{print $2}' | grep -v "^${PREFIX}" | grep -vi hdmi
}
detect_sink() {
    [ -n "$SINK" ] && { echo "$SINK"; return; }
    local s; s="$(pactl get-default-sink 2>/dev/null || true)"
    case "$s" in
        ${PREFIX}*) ;;                       # 默认就是音效 sink，不能用作物理声卡
        "") ;;
        *) echo "$s"; return ;;
    esac
    local -a cands; mapfile -t cands < <(list_physical)
    if [ "${#cands[@]}" -eq 1 ]; then echo "${cands[0]}"; return; fi
    if [ "${#cands[@]}" -gt 1 ]; then
        echo "检测到多个物理输出，请选一个（或 Ctrl-C 后用 --sink 指定）:" >&2
        local i=0
        for c in "${cands[@]}"; do echo "  [$i] $c" >&2; i=$((i+1)); done
        read -rp "序号: " pick >&2
        echo "${cands[${pick:-0}]}"; return
    fi
    echo ""
}
say "检测物理声卡"
HWSINK="$(detect_sink)"
if [ -z "$HWSINK" ]; then
    warn "没有找到物理输出设备 —— 请插好声卡后用 --sink <名字> 重新运行"
    exit 1
fi
say "物理声卡: $HWSINK"

# ---------------------------------------------------------------- 2. 装文件
say "安装文件到 $DATA"
run "mkdir -p '$DATA' '$DATA/ir' '$BINDIR' '$CONFDIR'"
[ "$SERVICES" = 1 ] && run "mkdir -p '$UNITDIR'"
run "cp -f '$REPO'/lib/*.py '$REPO'/lib/*.html '$DATA/'"
run "cp -f '$REPO'/lib/presets.tsv '$DATA/presets.tsv'"
run "cp -f '$REPO'/bin/yinxiao '$BINDIR/yinxiao'"
run "chmod +x '$BINDIR/yinxiao'"
run "cp -f '$REPO/README.md' '$REPO/docs/GUIDE.md' '$DATA/'"
if [ "$SERVICES" = 1 ]; then
    run "cp -f '$REPO'/systemd/*.service '$UNITDIR/'"
fi
run "printf '%s\n' '$HWSINK' > '$DATA/hw-sink'"
case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *) warn "$BINDIR 不在 PATH 里，需要自己加一下才能直接敲 yinxiao" ;;
esac

# ---------------------------------------------------------------- 3. 生成预设
say "生成 17 个 EQ / 矩阵型预设"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
run "python3 '$DATA/gen_presets.py' '$STAGE/pipewire/pipewire.conf.d' '$HWSINK'"
run "cp -f '$STAGE/pipewire/pipewire.conf.d/'*.conf '$CONFDIR/'"
run "cp -f '$STAGE/pipewire/presets.tsv' '$DATA/presets.tsv'"

say "合成 4 组厅堂脉冲响应（几秒 ~ 一分钟）"
run "python3 '$DATA/gen_irs.py'"

say "生成 4 个厅堂混响预设"
run "python3 '$DATA/gen_reverb.py'"

# ---------------------------------------------------------------- 4. 验证
say "验证峰值增益（σmax ≤ 1 才不会削顶）"
if [ "$DRY" = 0 ]; then
    if python3 "$DATA/eval_graph.py" "$CONFDIR" | tail -6; then :; else
        warn "验证器报了异常，请看上面的输出"
    fi
fi

# ---------------------------------------------------------------- 5. 服务
if [ "$SERVICES" = 1 ]; then
    say "安装并启动 systemd user 服务"
    run "systemctl --user daemon-reload"
    run "systemctl --user enable --now yinxiao-heal.service"
    run "systemctl --user enable --now yinxiao-editor.service"
    if ! loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -q yes; then
        warn "未开启 linger：服务只在登录后启动。想让它在开机就起（不用登录桌面）："
        warn "  sudo loginctl enable-linger $USER"
    fi
fi

say "重载 PipeWire"
# 重启会让 WirePlumber 重新挑默认输出，先把用户当前的选择记下来，之后原样恢复
PREV_SINK="$(pactl get-default-sink 2>/dev/null || true)"
run "systemctl --user restart pipewire pipewire-pulse wireplumber || systemctl --user restart pipewire pipewire-pulse"
if [ "$DRY" = 0 ] && [ -n "$PREV_SINK" ]; then
    for _ in $(seq 1 20); do
        pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -qx "$PREV_SINK" && break
        sleep 0.5
    done
    if pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -qx "$PREV_SINK"; then
        for _ in 1 2 3; do
            pactl set-default-sink "$PREV_SINK" 2>/dev/null || true
            [ "$(pactl get-default-sink 2>/dev/null)" = "$PREV_SINK" ] && break
            sleep 1
        done
        say "默认输出已恢复为: $PREV_SINK"
    fi
fi

cat <<EOF

安装完成。常用命令：

  yinxiao list            列出全部 21 个预设
  yinxiao 演唱会           切到某个预设（支持 id / 中文名 / 前缀）
  yinxiao off             关掉音效，直通物理声卡
  yinxiao status          看状态
  yinxiao edit            打开网页编辑器（http://127.0.0.1:8787）
  yinxiao fix / heal      修复链路 / 守护 DAC 掉线重连

物理声卡已记录在 $DATA/hw-sink
换声卡后重跑：bash install.sh --sink <新的 sink 名>
卸载：bash uninstall.sh
EOF
