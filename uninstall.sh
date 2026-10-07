#!/usr/bin/env bash
# yinxiao —— 卸载：停服务、删预设 conf、删命令行
# 默认保留 ~/.local/share/yinxiao（含你改过的参数与 IR），加 --purge 才一并删掉。
set -euo pipefail

DATA="${XDG_DATA_HOME:-$HOME/.local/share}/yinxiao"
BINDIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONFDIR="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

say() { printf '\033[36m▶\033[0m %s\n' "$*"; }

say "停止并禁用服务"
systemctl --user disable --now yinxiao-heal.service 2>/dev/null || true
systemctl --user disable --now yinxiao-editor.service 2>/dev/null || true
rm -f "$UNITDIR/yinxiao-heal.service" "$UNITDIR/yinxiao-editor.service"
systemctl --user daemon-reload 2>/dev/null || true

say "恢复直通（默认输出切回物理声卡）"
if [ -r "$DATA/hw-sink" ]; then
    HW="$(head -1 "$DATA/hw-sink")"
    if pactl list short sinks | awk '{print $2}' | grep -qx "$HW"; then
        pactl set-default-sink "$HW" 2>/dev/null || true
        for si in $(pactl list short sink-inputs | awk '{print $1}'); do
            pactl move-sink-input "$si" "$HW" 2>/dev/null || true
        done
    else
        echo "  （记录里的物理声卡 $HW 当前不在，手动切一下默认输出）"
    fi
fi

say "删除预设 conf"
rm -f "$CONFDIR"/50-yinxiao-*.conf
say "删除命令行"
rm -f "$BINDIR/yinxiao"

if [ "$PURGE" = 1 ]; then
    say "删除 $DATA（含 IR 与 hw-sink）"
    rm -rf "$DATA"
else
    echo "  保留 $DATA（含 IR / hw-sink / 你自己改过的参数）；要一并删掉加 --purge"
fi

say "重载 PipeWire"
systemctl --user restart pipewire pipewire-pulse wireplumber 2>/dev/null \
    || systemctl --user restart pipewire pipewire-pulse 2>/dev/null || true
echo "卸载完成。"
