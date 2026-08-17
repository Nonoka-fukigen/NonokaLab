# Nonoka Lab 架构说明

> 设计理念：**Plugin Freedom, User Sovereignty**（插件自由，用户主权）。
> 本文说明 Core 包结构、事件总线、服务定位器、插件路径优先级与生命周期。

## 1. 分层总览

```
┌──────────────────────────────────────────────────────┐
│  用户插件（视频下载 / 未来更多）                        │  手动启停、懒加载
│  （文档/NonokaLab/plugins、dev_plugins）               │
├──────────────────────────────────────────────────────┤
│  内置插件（随安装包分发，安装目录，只读）                │
│  插件管理器 / 设置页 / 系统托盘 / 组件管理器 / 通知…      │
├──────────────────────────────────────────────────────┤
│  Core（nonoka_shell/core/）                           │  只提供规则，无业务逻辑
│    event_bus / plugin_base / plugin_manager /         │
│    service_locator / config                           │
├──────────────────────────────────────────────────────┤
│  基础设施（logger / database / registry / utils …）   │
└──────────────────────────────────────────────────────┘
```

## 2. Core 包结构（nonoka_shell/core/）

| 文件 | 职责 |
|------|------|
| `event_bus.py` | 异步事件总线：订阅/发布，emit 带 trace_id，事件记录供「事件追踪面板」 |
| `plugin_base.py` | `Plugin` 基类：唯一契约（activate / deactivate / migrate_config / service / emit） |
| `plugin_manager.py` | `CorePluginManager`：三目录扫描、依赖图（拓扑排序 + 环检测）、core_version_min 检查、懒加载、状态机（running/stopped/paused/crashed）、心跳 |
| `service_locator.py` | `ServiceLocator`：插件间同步服务调用的唯一通道 |
| `config.py` | JSON 配置读写（Core 自包含最小实现） |
| `__init__.py` | 导出 + `python -m nonoka_shell.core --test` 无窗口自检 |

> 兼容层：`nonoka_shell/core.py` 仅 re-export（`Core == CorePluginManager`），推荐直接导入 `from nonoka_shell.core import ...`。

## 3. 事件总线（异步）与服务定位器（同步）—— 混合模式

**禁止插件之间直接 import 或直接调用函数**。两类通信各走各的通道：

| 场景 | 通道 | 说明 |
|------|------|------|
| 异步事件通知（状态变化、下载完成、组件安装完成、崩溃） | `core.bus.emit(topic, data)` / `core.bus.on(topic, cb)` | 订阅方收到 `(data, trace_id)` 后更新 UI 或执行后续动作；不阻塞调用方 |
| 同步服务调用（设置页取插件列表、托盘停止插件等需要立即返回的场景） | `core.register_service(plugin_id, svc)` / `core.get_service(plugin_id)` | 插件在 `activate()` 中注册服务，其它插件通过 `core.get_service(plugin_id)` 获取并同步调用 |

插件基类提供便捷方法：

```python
class MyPlugin(NonokaPlugin):
    def on_start(self):
        # 异步通知
        self.emit("my_plugin.ready", {"v": 1})
        # 同步调用其它插件服务
        svc = self.service("some_plugin")
        if svc is not None:
            svc.do_something()
```

## 4. 插件路径与优先级

| 类型 | 路径 | 读写 | 说明 |
|------|------|------|------|
| 内置插件 | 安装目录 `plugins/`（仓库根） | 只读 | 随安装包分发，不可卸载/禁用 |
| 市场插件 | `文档/NonokaLab/plugins/` | 可读写 | 从插件市场安装 |
| 开发者插件 | `文档/NonokaLab/dev_plugins/` | 可读写 | 开发者模式，支持热重载 |

扫描顺序：**内置 > 市场 > 开发者**；ID 冲突时优先级高的目录生效（后扫描目录不覆盖已存在条目）。
每个插件元数据带 `source`（builtin / market / dev）字段，`list_meta()` 透出给前端。

## 5. 插件生命周期与状态

```
stopped ──(用户启动)──▶ running ──(暂停)──▶ paused ──(恢复)──▶ running
   ▲                       │  ▲
   └────(停止/任务完成)──────┘  └──(崩溃)──▶ crashed ──(重启)──▶ running
```

- **懒加载**：不启动不 import；列表/市场展示只用 `plugin.json` 元数据。
- **状态持久化**：`plugin_status.running_status` 记录；按「恢复上次运行的插件」开关（默认关）决定是否自动恢复。
- **崩溃恢复**：插件线程未捕获异常（`threading.excepthook`）或 30s 无心跳 → 标记 `crashed` + 桌面通知；
  「插件崩溃后自动重启」开关（默认关）开启后自动重启。
- **关闭窗口不停插件**；彻底退出（托盘菜单）时 `stop_all()`。

## 6. 兼容性检查（core_version_min）

扫描时检查 `plugin.json.core_version_min` 与 Core 版本（`core.CORE_VERSION` / `brand.VERSION`）：
不符合 → 标为「不兼容」，不加载，插件管理页显示原因，导航隐藏（开发者模式可见）。

## 7. 插件数据与卸载边界

- 插件数据统一存放：`文档/NonokaLab/plugins_data/<plugin_id>/`。
- 卸载时前端询问「是否同时删除该插件的所有数据？」；是 → 删除，否 → 保留（重装可恢复）。
- 基础插件（system）数据永不删除，只能「恢复默认设置」。

## 8. 显示层级

- **默认模式**：导航只显示视频下载插件；设置页仅通用项（主题 / 恢复开关 / 语言 / 更新 / 关于）。
- **开发者模式**：连续点击关于页版本号 3 次触发（带抖动视觉反馈），再点 3 次退出；
  显示全部插件、数据/开发者导航组、高级设置分组与红色警告条。

## 9. 日志与隐私

- 日志路由：`nonoka.log`（Core 核心）、`plugin_<id>.log`（插件）、`crash_report.json`（崩溃）。
- 全局单调序号 `[NNNN]`；异步缓冲（上限 1000，满时丢 DEBUG）；DEBUG 仅开发者模式；5MB×3 轮转。
- **隐私**：反馈与崩溃上报默认**不含**下载 URL / 视频标题 / 日志正文；用户勾选「包含详细日志」才附加。
- 事件追踪面板（开发者模式）：读取 `EventBus.trace()` 实时展示插件间事件通信。

## 10. 无窗口自检

Core 逻辑不依赖窗口，可命令行自检：

```bash
python -m nonoka_shell.core --test
```

## 11. 版本管理

### 11.1 壳子与插件版本独立维护

Nonoka Lab 遵循**语义化版本（SemVer）**：`主版本.次版本.修订号`（如 `1.2.3`）。

- **壳子版本**：记录在 `nonoka_shell/brand.py` 的 `VERSION`，同时体现于关于页、安装包文件名与 Release 标签。
- **插件版本**：记录在插件自己的 `plugin.json` 的 `version`，独立于壳子版本维护。

两者互不影响：壳子升级次版本不要求插件升级；插件升级也不影响壳子版本号。

### 11.2 版本位规则

**壳子**：

| 版本位 | 何时 +1 |
|--------|---------|
| 主版本 | 架构级不兼容改动（插件契约大变、数据结构不兼容） |
| 次版本 | 新增功能，向后兼容（新增内置插件、新增系统能力） |
| 修订号 | 修复 bug，无新功能 |

**插件**（规则一致，作用域为插件自身）：

| 版本位 | 何时 +1 |
|--------|---------|
| 主版本 | 插件架构不兼容改动（通信契约变化，旧接口不可用） |
| 次版本 | 插件新增功能，向后兼容 |
| 修订号 | 插件修复 bug |

### 11.3 初始版本约定

- 开发阶段建议使用 `0.x.x`（如 `0.9.0`），此时 API 可随时变化，不保证向后兼容。
- 首个对外稳定版本为 `1.0.0`，此后严格按 SemVer 递增。
- 当前项目尚未正式发布，壳子与 Core 版本统一保持 `1.0.0`。

### 11.4 发布流程

1. **更新版本号**：
   - 壳子：修改 `nonoka_shell/brand.py` 的 `VERSION`（Core 版本 `nonoka_shell/core/__init__.py` 的 `CORE_VERSION` 同步保持一致）。
   - 插件：修改插件目录 `plugin.json` 的 `version`。
2. **记录变更**：在 `CHANGELOG.md` 追加本次变更说明。
3. **打标签**：推送 Git 标签 `vX.Y.Z`（如 `v1.1.0`）。
4. **自动发布**：GitHub Actions 检测到 `vX.Y.Z` 标签后自动执行：
   PyInstaller 打包 → Inno Setup 生成 `NonokaLab_Setup_vX.Y.Z.exe` → 上传 Release 资产。

### 11.5 标签命名规范

- 统一为 `vX.Y.Z`（小写 `v` + 语义化版本），如 `v1.0.0`、`v1.1.0`。
- 版本解析用正则提取首个 `X.Y.Z[.N]` 段（兼容 `release-2.0.1-beta` 之类前缀/后缀），四段补零比较，避免 `1.0.0` vs `1.0` 误判。

