# Nonoka Lab 全局 UI 设计规范

> 本文档是 **shell 前端** 与 **所有插件前端** 的样式与交互唯一权威依据。
> **任何新增/修改 UI 前必须先阅读本文档**，并优先复用全局设计系统，不自行发明样式。

---

## 1. 设计语言

- **风格**：Cupertino（iOS）—— 圆角卡片、毛玻璃导航、线性图标、系统字体。
- **强调色**：`#ffbbcc`（以下统一称 **FFBBCC**），所有主色、选中态、运行态一律使用它。
- **字体**：系统字体栈（见 `--font`），不引入 webfont。
- **按钮**：所有按钮组**居中**展示。

### 1.1 核心文件

| 文件 | 作用 |
| --- | --- |
| `frontend/css/cupertino.css` | 全局设计系统（shell 与插件共用），定义 CSS 变量与通用组件 |
| `frontend/js/page.js` | 页面共享逻辑（RPC 中继、弹窗、图标、多语言） |
| `plugins/<name>/frontend/css/app.css` | 插件专属样式，**必须**复用 cupertino 的 CSS 变量 |
| `plugins/<name>/frontend/index.html` | 插件页面，引用 `../../../frontend/css/cupertino.css` |

> 插件 HTML 引用全局样式路径固定为：`../../../frontend/css/cupertino.css`（相对插件目录）。

--- 

## 2. 主题变量（cupertino.css `:root`）

```css
--accent: #ffbbcc;        /* 主色：FFBBCC */
--accent-press: #f7a8c0;  /* 按压态（更深粉） */
--accent-soft: #ffe6ee;   /* 浅粉底（hover/选中底） */
--label:  #1c1c1e;        /* 主文字 */
--label-2:#8e8e93;        /* 次要文字 */
--bg: #f2f2f7;            /* 页面背景 */
--bg-elevated:#ffffff;    /* 卡片背景 */
--fill: #e9e9eb;          /* 中性填充 */
--fill-2:#f2f2f7;         /* 输入框底 */
--radius:16px; --radius-sm:11px; --radius-xs:8px;
```

**规则**：任何颜色都不要写死具体值，一律用 `var(--accent)` 等变量；深浅色主题由变量自动切换。

---

## 3. 按钮规范

按钮只有三种形态，**只通过 class 区分**：

| Class | 样式 | 用途 |
| --- | --- | --- |
| `.btn`（默认） | **FFBBCC 边框 + FFBBCC 字 + 白底**（描边） | 常规操作按钮 |
| `.btn.primary` | **FFBBCC 填充 + 白字** | 主要/启动类操作（启动、开始下载、解析、开始使用、安装、提交） |
| `.btn.gray` / `.btn.ghost` | 同 `.btn`（FFBBCC 描边） | 次要按钮，同样跟随主题，不再使用灰色 |

- 小号变体：`.btn.sm`（仅在紧凑行内使用，如历史列表）。
- 禁用态：`.btn[disabled]` 半透明。
- hover：描边按钮用 `--accent-soft` 底；primary 用 `--accent-press`。
- `white-space: nowrap` 已默认开启，文字**禁止竖排/换行**。
- **按钮文本永远是横排**；若按钮过窄导致换行，请增大宽度而非去掉 nowrap。

```html
<button class="btn">关闭</button>
<button class="btn primary">开始下载</button>
<button class="btn gray">Cookie 说明</button>
```

---

## 4. 分段控件（seg：平台/标签/模式选择）

`.seg` 是「哔哩哔哩/抖音」「下载视频/下载封面」「仅视频/仅音频/视频+音频」这类选择器。

| 状态 | 样式 |
| --- | --- |
| `.seg` 容器 | **FFBBCC 边框 + 白底** |
| `.seg button`（未选中） | **FFBBCC 字 + 透明底** |
| `.seg button.on`（选中） | **FFBBCC 填充 + 白字** |

```css
.seg { background:#fff; border:1px solid var(--accent); ... }
.seg button { color:var(--accent); ... }
.seg button.on { background:var(--accent); color:#fff; }
```

--- 

## 5. 状态标签（tag）

| Class | 语义 | 样式 |
| --- | --- | --- |
| `.tag.wait` | 等待/未运行 | **FFBBCC 字 + FFBBCC 边框 + 白底** |
| `.tag.run` / `.tag.ok` | 运行/成功 | **FFBBCC 填充 + 白字** |
| `.tag.no` | 失败/错误 | 红色（保留语义色） |

> 运行态一律 FFBBCC，**不使用绿色**；未运行态 FFBBCC 字+白底。

---

## 6. 布局约束

- **按钮组居中**：所有按钮行用 `.row.center` / `.btn-row` / `.actions`（自带 `justify-content:center`）。
- **保存位置等「输入框+按钮」行**：输入框 `flex:1` 占满剩余，按钮保持自身宽度（`.btn` 已 `flex:none`）。
- **弹窗（modal）**：`.modal-mask` 必须 `position:fixed`，保证滚动后仍居中。
- **卡片**：`.card` 用于分组，圆角 `--radius`。
- 插件页面排版：平台/标签选择居中，`seg-row` 用 `display:flex; justify-content:center`。

---

## 7. 插件开发强制规范

1. **必须**引用全局样式：`<link rel="stylesheet" href="../../../frontend/css/cupertino.css">`。
2. **必须**复用 `.btn` / `.seg` / `.tag` / `.card` / `.input` 等组件，不自行定义同功能样式。
3. 颜色一律用 `var(--accent)` 等变量，禁止硬编码其他颜色。
4. 所有按钮组居中；按钮文本横排。
5. 插件专属样式放 `app.css`，仅覆盖插件特有组件，不覆盖全局通用组件。
6. 新增插件前，先阅读本规范 + 参考 `Nonoka_video_download` 插件的实现。
7. 交互动效：hover 用 `--accent-soft`，primary 用 `--accent-press`，按压 `scale(0.97)`。

---

## 8. 验收清单

- [ ] 默认按钮 = FFBBCC 边框 + 字 + 白底
- [ ] 启动类按钮 = FFBBCC 填充 + 白字
- [ ] seg 选中项 = FFBBCC 填充 + 白字，未选中 = 描边
- [ ] 运行态 = FFBBCC 填充，未运行 = FFBBCC 字白底
- [ ] 按钮组居中、文字横排
- [ ] 深浅色主题都正常
- [ ] 文件夹/文件选择对话框置顶弹出