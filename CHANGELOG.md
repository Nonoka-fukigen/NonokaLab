# Changelog

本文件遵循 [语义化版本](https://semver.org/lang/zh-CN/)（MAJOR.MINOR.PATCH）。
所有发布均通过 GitHub Actions 在推送 `vX.Y.Z` 标签时自动构建安装包
`NonokaLab_Setup_vX.Y.Z.exe`。

## [1.0.1] — 2026-08-17 关于页完善、设置清理、插件状态修复、FFmpeg 响应式检测

### 关于页面功能完善

- **检查更新**：点击后从 GitHub API 获取最新版本，有新版本时弹窗提示并提供带进度条的下载功能
- **反馈问题**：点击后直接跳转浏览器打开 GitHub Issues 页面
- **关于弹窗**：点击后弹出模态框显示项目信息（名称、版本、作者、GitHub 链接、许可证等）

### 设置页面清理与优化

- 删除设置中冗余的「反馈」表单板块（与关于页重复），彻底清除相关代码
- 将左侧导航栏「设置」更名为「通用」，页面内部标题同步修改

### 插件状态同步机制（Shell 统一处理）

- 修复插件页面切换后状态丢失的问题
- 切换页面再返回，插件状态正确显示为「运行中」或「未运行」
- 所有现有和未来插件自动获得此能力，无需单独处理

### FFmpeg 检测状态响应式更新

- 修复插件启动后 FFmpeg 警告提示不自动消失的问题
- 启动/停止插件时，FFmpeg 状态检测自动刷新，UI 立即更新，无需手动切换页面

## [1.2.0] — 2026-08-16 最终重构：Core 拆分 + 双模式 + 崩溃恢复 + 隐私

### Core 拆分为包（nonoka_shell/core/）

- `event_bus.py`（异步事件总线，trace_id 链路 + 事件追踪面板数据）
- `plugin_base.py`（插件基类）
- `plugin_manager.py`（CorePluginManager：三目录扫描、依赖图、core_version_min、懒加载、状态机含 crashed、心跳）
- `service_locator.py`（同步服务定位器：插件间同步调用唯一通道）
- `config.py`（JSON 配置）；`python -m nonoka_shell.core --test` 无窗口自检
- 插件间**禁止直接 import / 调用函数**：异步走 EventBus、同步走 ServiceLocator

### 插件契约与路径

- `plugin.json` 新增 `core_version_min`：不符合标为「不兼容」，不加载并显示原因
- 三目录扫描：内置（安装目录，只读）> 市场（文档/NonokaLab/plugins）> 开发者（dev_plugins）；ID 冲突内置优先
- 插件数据统一存放 `文档/NonokaLab/plugins_data/<id>/`；卸载时询问是否删除数据
- 基础插件（system）不可卸载/禁用；提供「恢复默认设置」

### 生命周期与崩溃恢复

- 恢复上次运行的插件开关（默认关，普通设置可改）
- 崩溃检测：线程未捕获异常 / 30s 无心跳 → 标记 crashed + 桌面通知 + 查看错误日志/重启
- 「插件崩溃后自动重启」开关（默认关）

### 显示层级（默认 / 开发者双模式）

- 默认模式：导航仅视频下载插件；设置页仅通用项（主题/恢复开关/语言/更新/关于）
- 开发者模式：连续点击关于页版本号 3 次触发（抖动视觉反馈），再点 3 次退出
  - 显示全部插件 + 数据/开发者导航 + 高级设置分组 + 红色警告条 + 恢复默认设置
- 事件追踪面板（开发者模式）：实时展示 EventBus 事件通信

### 日志与隐私

- 日志简化：nonoka.log（Core）/ plugin_<id>.log（插件）/ crash_report.json（崩溃）；DEBUG 仅开发者模式
- **隐私**：反馈与崩溃上报默认不含下载 URL / 视频标题 / 日志正文；勾选「包含详细日志」才附加
- README 明确「下载记录和链接永远不会被自动上传」

### 文档

- 新增 `docs/architecture.md`；README 更新开发者模式彩蛋 / 恢复开关 / 崩溃恢复 / 插件数据 / 路径优先级 / 同步异步 / 隐私章节

## [1.1.0] — 2026-08-16 终极重构：Plugin Freedom, User Sovereignty

架构升级为「插件自由，用户主权」：极简 Core 只提供规则，一切能力皆插件。

### 极简 Core（`nonoka_shell/core.py`，约 100 行）

- `EventBus`：插件间通信唯一通道，每次 `emit` 携带 8 位 `trace_id`（链路追踪）。
- `Plugin` 基类：统一契约（`activate` / `deactivate` / `migrate_config`）。
- `Core`：扫描（只读 `plugin.json`）→ 拓扑排序 → DFS 环检测 → 懒加载 → 心跳监控；
  不含任何业务逻辑。

### 插件契约与依赖管理

- `plugin.json` 扩展：`provides` / `consumes` / `permissions` / `sha256` / `config_version`。
- 卸载前依赖检查：有插件依赖时返回 `need_confirm`，用户确认才卸载。
- 循环依赖检测：扫描时 DFS 环检测并报错标记。
- 配置迁移：`config_version` 变化调用插件 `migrate_config(old, new)`；未实现则保留旧配置并警告。
- 市场安全校验：安装 / 更新下载 zip 后计算 SHA256，不符拒绝安装并提示「可能已被篡改」。

### 生命周期与状态

- **懒加载**：不启动的插件不 import、不占内存；列表 / 市场展示只用元数据。
- **状态持久化恢复**：`plugin_status.running_status` 记录手动启动状态，Core 启动时 `restore_running()` 恢复。
- **心跳监控**：运行中插件每 5s 写心跳日志；30s 无心跳标记 🟡 无响应、120s 标记 🔴 卡死；
  设置页 / 开发者栏显示健康圆点。

### 后台优先运行（窗口懒创建）

- 双击 exe → Core + 托盘（不创建窗口）；点击托盘图标才懒创建 pywebview 窗口。
- 关闭窗口拦截 `closing` → `hide()`（插件继续后台运行）；彻底退出仅通过托盘「退出」。
- 平台不支持无窗口时自动回退「隐藏窗口」模式，保证可用。

### 日志系统（借鉴计算机网络设计）

- 分层：L1 基础设施 / L2 系统 / L3 事件总线 / L4 应用与插件，每层独立文件
  （`l1_infra.log` / `l2_system.log` / `l3_bus.log` / `l4_app.log` / `plugin_<id>.log` / `nonoka.log`）。
- 全局单调序号 `[NNNN]`（跳号即日志丢失）；异步缓冲 + 流量控制（上限 1000，满时丢 DEBUG）。
- Trace ID 链路日志（`[trace=xxxx] emit topic`）；文件轮转 5MB×3。

### 注册表自清洁（`nonoka_shell/registry.py`）

- 所有注册表操作集中管理，只使用 HKCU；写入记录到 DB（`registry_keys` 表）。
- 启动检查旧版本残留；卸载 / 退出删除所有自建键；自启键写入与删除统一封装。

### 其它

- `main.py` 重构为后台优先入口；`bridge.get_health()` 与 `list_meta().health` 提供健康状态；
- 前端：插件卡片健康圆点、卸载依赖确认交互；`developer.html` 同步；
- README 重写为 26 章节；`docs/plugin_development.md` 补充生命周期 / 迁移 / sha256 / 依赖章节。

## [1.0.0] — 2026-08-16

首个正式版本。在既有视频下载核心基础上，补齐「插件化桌面工具箱」所需的全套壳子能力。

### 新增功能

- **数据库（SQLite）**：`nonoka_shell/database.py`，落盘 `用户文档/NonokaLab/data/nonoka.db`，
  记录下载历史（`download_history`）、插件状态（`plugin_status`，含 `running_status`）、键值配置（`settings`）。
- **壳子自动更新**：`updater.py` 启动后后台比对 GitHub Release 版本，发现新版弹窗提示并下载安装包到
  `用户文档/NonokaLab/updates/`（软件不自覆盖，下载后引导用户运行安装包）。
- **插件自动更新 / 市场**：`plugin_manager.py` 后台检查各插件 `plugin.json` 的 `repo` 版本并标记「有新版本」；
  设置页「插件市场」支持一键安装 / 更新 / 卸载远程插件（内置插件不可卸载）。
- **用户反馈**：`feedback.py` + 设置/关于页弹窗，提交后跳转 GitHub Issues 并预填内容（可选附日志）。
- **崩溃上报**：`crash_report.py` + `error_handler.py` 全局异常捕获，生成 `logs/crash_report.json` 并弹窗询问是否上报。
- **多语言**：`i18n.py` + `frontend/locales/zh.json`、`en.json`，设置页切换中文 / English，首次启动按系统语言自动选择。
- **首次引导**：`index.html` 欢迎遮罩，介绍产品、提示安装 ffmpeg、引导去设置；`welcome_done` 持久化。
- **系统托盘**：`tray.py`（pystray）。左键显示主窗口；右键菜单「显示主窗口 / 停止全部插件 / 退出」；
  关闭窗口默认最小化到托盘（不退出），完全退出时停止所有运行中的插件。
- **桌面通知**：`notifier.py`（plyer → win10toast 兜底）。下载完成 / 插件更新 / 壳子更新 / 插件启停 / 剪贴板检测等事件通知；开关默认开。
- **下载历史页**：`frontend/pages/history.html`，列表、搜索、打开文件夹、删除、重新下载、清空、导出 CSV。
- **深色模式**：`cupertino.css` 通过 `<html data-theme>` 支持 浅色 / 深色 / 跟随系统（默认跟随系统）。
- **任务队列**：`queue_manager.py`。下载 / 封面进入统一队列，支持并行数限制（默认 2）、排队顺序上移/下移、
  暂停/恢复/取消（第三方核心为阻塞式，取消/暂停为尽力而为）。
- **代理设置**：`config.py` + 桥接 `set_proxy` / `test_proxy`。支持 不使用 / HTTP / SOCKS5，含用户名密码，
  应用到 yt-dlp 与 requests；设置页提供「测试连接」。
- **剪贴板监听**：`clipboard_listener.py`（默认关）。检测 B站 / 抖音 链接，弹桌面通知并可一键跳转下载。
- **全局快捷键**：`hotkeys.py`（pynput，缺失则静默降级）。默认 `Ctrl+Shift+N` 显示窗口；可自定义或禁用。
- **数据导入 / 导出**：`backup.py`。导出 / 导入数据库 + 配置 + 插件（ZIP，带 zip-slip 防护）；手动选择位置。
- **开机自启**：`autostart.py`。写 Windows 注册表 Run 键（默认关；非 Windows 静默返回不支持）。
- **使用统计**：`stats.py`。本地统计下载数 / 活跃天数 / 已启用插件数 / 类型分布，关于页展示。
- **插件手动运行生命周期**：`plugin_manager.py` + `Plugin` 基类 `on_start/on_stop/on_pause/on_resume`。
  插件不随壳子启动自动运行；用户点击「启动」后运行，直到手动停止 / 自身完成 / 壳子退出；关闭窗口或切换页面不影响运行状态。
- **开发者插件栏**：`frontend/pages/developer.html`。扫描 `plugins/` 与 `用户文档/NonokaLab/dev_plugins/`，
  支持热重载、查看运行日志、启停；`dev_mode` 开关控制是否显示 dev 插件；附 `docs/plugin_development.md`。

### 版本管理

- `.gitignore`、`CHANGELOG.md`（本文件）、`LICENSE`（MIT）、`README.md`「版本管理」章节。
- `installer/setup.iss`（Inno Setup，含可选 FFmpeg 组件）与 `.github/workflows/release.yml`（PyInstaller + iscc 自动发布）。
