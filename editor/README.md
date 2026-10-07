# 前端源码与构建

`../lib/editor.html` 是**构建产物**（单文件，Tailwind + Preact/htm 全部内联，运行期零外部请求、
离线可用）。要改前端就改 `editor.src.html`，然后跑一次构建：

```bash
bash build.sh          # 需要 node（仅开发期）；会 npm install、编译 Tailwind、内联装配
```

* `editor.src.html` —— 源码。HTML 结构 + Tailwind 类 + Preact 组件（htm 模板字符串）
* `editor.tailwind.css` —— Tailwind 入口与主题 token（黑白两套用 CSS 变量 + `.dark` 类切换）
* `build.py` —— 内联装配脚本：`/*__TAILWIND__*/` 和 `/*__VENDOR__*/` 两个占位符分别替换成
  编译后的 CSS 与内联的库
* `vendor/` —— Preact / hooks / htm 的 UMD 构建（MIT，见 `vendor/licenses/`），
  内联进产物以保证离线可用

**装的时候不需要 node**：`install.sh` 只复制已经构建好的 `lib/editor.html`。

⚠️ 改 `editor.src.html` 时注意：htm 模板里**不能出现裸露的 `<`**（会被当成标签开头）。
比如要写「k<0」，得改成「k 为负」或用 `${'<'}` 插值——踩过一次：整页 UI 会在
`createElementNS('0')` 处崩掉。
