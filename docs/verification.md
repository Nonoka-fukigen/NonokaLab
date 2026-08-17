# Nonoka Lab 验证报告（v1.0.0 收尾 + v1.1.0 + v1.2.0）

验证时间：2026-08-16　执行方式：无头（headless）验证，不依赖 GUI

## v1.2.0 最终重构验证（Core 拆分 + 双模式 + 崩溃恢复 + 隐私）

| # | 检查项 | 结果 |
|---|--------|------|
| F1 | `python -m nonoka_shell.core --test`（5 项：EventBus/拓扑+环/ServiceLocator/兼容+优先级/崩溃） | ✅ |
| F2 | Core 包拆分：event_bus / plugin_base / plugin_manager / service_locator / config | ✅ 编译通过 |
| F3 | 三目录优先级：内置 > 市场 > 开发者（ID 冲突内置生效，source 正确） | ✅ |
| F4 | core_version_min：v9.9.9 要求 → 不兼容、不加载、原因可见、start 拒绝 | ✅ |
| F5 | ServiceLocator 同步调用（register/get/call，无直接 import） | ✅ |
| F6 | EventBus 异步事件 + trace_id + 事件面板 trace | ✅ |
| F7 | 崩溃标记 crashed + 桌面通知 + 原因 + restart + 自动重启调度 | ✅ |
| F8 | 恢复开关默认关（restore_running=False 不恢复；开启才恢复） | ✅ |
| F9 | 插件数据目录 plugins_data/<id>：has/delete；卸载返回 has_data | ✅ |
| F10 | 基础插件保护：system 不可禁用/卸载；restore_base_settings | ✅ |
| F11 | 日志简化路由：nonoka.log + plugin_<id>.log（无分层文件） | ✅ |
| F12 | 隐私：崩溃报告默认不含 recent_log；勾选才附加 | ✅ |
| F13 | locale 155 键零缺失；bridge 62 个调用全部命中（含新 API） | ✅ |
| F14 | py_compile 全过 + node --check 全过 | ✅ |

## v1.1.0 终极重构验证（Plugin Freedom, User Sovereignty）

| # | 检查项 | 结果 |
|---|--------|------|
| R1 | `core.py` 极简内核（EventBus / Plugin / Core）编译 | ✅ |
| R2 | EventBus：emit 携带 8 位 trace_id，订阅回调收到 (data, trace_id) | ✅ |
| R3 | 拓扑排序：A←B←C → [A,B,C]（被依赖者先） | ✅ |
| R4 | DFS 环检测：A→B→A 返回 [A,B,A]，不无限递归（toposort 遇环跳过） | ✅ |
| R5 | `dependents()` 依赖查询（卸载前检查） | ✅ |
| R6 | 插件懒加载：load_all 只读元数据，`plugins` 为空（未 import） | ✅ |
| R7 | 生命周期状态机 + 心跳：start→🟢ok / 无心跳→🔴dead | ✅ |
| R8 | 状态持久化恢复：`restore_running()` 自动重新 activate | ✅ |
| R9 | 配置迁移：`migrate_config(1,2)` 被调用，config_version 落库 | ✅ |
| R10 | sha256 校验：正确哈希通过、错误哈希拒绝 | ✅ |
| R11 | 卸载依赖检查：`need_confirm` 返回依赖者列表；force 可继续 | ✅ |
| R12 | 日志分层路由：nonoka.log / l1-l4 / plugin_<id>.log 各归其位 | ✅ |
| R13 | 日志全局序号 `[NNNN]` 连续；Trace ID `[trace=xxxx]` 落 l3_bus.log | ✅ |
| R14 | registry 平台安全：非 Windows 写入/删除/自启 no-op 不崩溃 | ✅ |
| R15 | locale 完整性：141 个引用键 zh/en 零缺失，新增 8 键齐备 | ✅ |
| R16 | 前端 bridge 契约：56 个调用全部命中；新增 `get_health` 存在 | ✅ |
| R17 | `.gitignore` / `LICENSE`（MIT）/ `CHANGELOG.md`（v1.1.0） | ✅ |

## v1.0.0 收尾验证

## 验证结果总览

| # | 检查项 | 结果 |
|---|--------|------|
| 1 | 全部 Python 模块 `py_compile` | ✅ 通过 |
| 2 | 全部前端 JS `node --check` | ✅ 通过 |
| 3 | 语言文件 JSON 合法性（zh / en） | ✅ 通过 |
| 4 | 前端 `T("...")` 键完整性（138 个引用键） | ✅ 无缺失 |
| 5 | 后端 i18n `t("...")` 键完整性 | ✅ 无缺失 |
| 6 | 插件加载（plugins/ 扫描 + 热重载 reload 分支） | ✅ 通过 |
| 7 | 插件生命周期状态机 stopped→running→(paused→running)→stopped | ✅ 通过 |
| 8 | 状态变化回调 on_state 推送 | ✅ 4 次全部推送 |
| 9 | 启动/停止桌面通知（zh 本地化解析） | ✅ 已解析为「插件已启动 / 插件已停止」 |
| 10 | 下载/封面经 QueueManager 提交（plugin→queue routing） | ✅ download + cover 均入队 |
| 11 | 前端 bridge 调用契约（56 个调用 vs bridge 76 个方法） | ✅ 无缺失、无错名 |
| 12 | main.py 对 NonokaBridge 的引用 | ✅ 一致 |

## 本轮修复的问题

1. **`plugin_manager.py::_load_one` importlib 作用域 bug（阻塞性）**
   - 症状：加载任何插件都抛 `cannot access local variable 'importlib' where it is not associated with a value`，插件全部加载失败。
   - 根因：`import importlib` 只写在 `if reload and mod_name in sys.modules:` 分支内，Python 将 `importlib` 视为整个函数的局部变量；正常加载（`reload=False`）走 `else` 分支时 `importlib` 未绑定，访问 `importlib.util.spec_from_file_location` 即报错。
   - 修复：在 `_load_one` 函数体开头无条件 `import importlib`，并移除分支内冗余导入。
   - 验证：正常加载与 `reload_plugin` 热重载分支均通过。

2. **locale 键缺失（`nav_history` / `nav_queue`）**
   - `shell.js` 的 `buildNav`/`selectShell` 引用这两个键，但 zh/en 均未定义，会显示原始键名。
   - 修复：zh（下载历史 / 任务队列）、en（Download history / Task queue）已补齐。

3. **locale 重复键 `plugin_stopped`**
   - 同一键同时承担「状态徽标（已停止）」与「停止通知（插件已停止）」，后者覆盖前者导致状态徽标显示错误文案。
   - 修复：状态键保留 `plugin_stopped`；通知键改为 `plugin_stopped_toast`，并同步更新 `plugin_manager.py` 中的引用。

## 说明

- 核心下载代码（`bilibili_downloader.py` / `douyin_downloader.py` / `douyin_browser.py`）未做任何改动。
- 下载类任务验证到「正确提交至任务队列」为止，未触发真实网络下载；真实下载路径由既有核心承担（已在历史会话验证过）。
- 托盘、通知、快捷键、剪贴板监听等 GUI 相关子系统在无头环境不可实例化，采用「模块可导入 + 桥接方法存在 + 状态机/契约逻辑单测」方式覆盖，建议在真实桌面环境跑一次冒烟。
