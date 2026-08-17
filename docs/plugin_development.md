# Nonoka Lab 插件开发指南

本文介绍如何为 Nonoka Lab 编写插件。壳子已提供数据库、自动更新、系统托盘、桌面通知、任务队列、
代理、剪贴板监听、全局快捷键、数据导入导出、开机自启、使用统计等通用能力，插件只需关注自身业务逻辑。

---

## 1. 目录结构

内置插件放在 `plugins/<your_plugin_id>/`；开发者插件放在
`用户文档/NonokaLab/dev_plugins/<your_plugin_id>/`（开启「开发者模式」后可见，支持热重载）。

```
plugins/MyPlugin/
├── plugin.py          # 必须：插件入口，继承 NonokaPlugin
├── plugin.json        # 必须：元数据
├── core/              # 可选：私有核心代码（原样 import，不改逻辑）
└── frontend/          # 可选：插件前端（Cupertino 风格）
    ├── index.html
    ├── css/app.css
    └── js/app.js
```

> 开发者插件的 `core/` 必须通过 `sys.path.insert(0, core_dir)` 加入搜索路径，
> 再以原始顶层模块名 import（与既有脚本保持一致，不改动核心代码）。

---

## 2. 插件契约（plugin.py）

```python
from nonoka_shell.plugin_manager import NonokaPlugin

class Plugin(NonokaPlugin):
    id = "MyPlugin"
    name = "我的插件"
    icon = "puzzle"                 # 对应 frontend/assets/icons.svg 的 symbol id
    description = "一句话描述"

    def api_methods(self):
        # 暴露给前端的方法名列表（其余方法不暴露）
        return ["do_something", "get_status"]

    def frontend_path(self):
        import os
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "index.html")

    # 生命周期（手动启动/停止）
    def on_load(self):   ...        # 加载时（一次）
    def on_start(self):  ...        # 用户点「启动」后
    def on_stop(self):   ...        # 用户点「停止」/ 退出 时
    def on_pause(self):  ...        # 暂停（可选）
    def on_resume(self): ...        # 恢复（可选）
    def on_unload(self): ...        # 卸载/退出时

    # 暴露给前端的方法
    def do_something(self, payload):
        ...
```

生命周期状态机：`stopped → running → (paused → running) → stopped`。
**插件不随壳子启动而自动运行**；用户点击「启动」后才进入 `running`，直到手动停止、自身完成或壳子退出。
关闭主窗口（最小化到托盘）或切换页面都**不会**停止插件。

**状态持久化与恢复**：用户手动启动的插件会记录到 `plugin_status.running_status`；
下次 Core 启动扫描后自动恢复上次为 running 的插件（重新执行 `on_start`，异常会捕获并记入日志）。

**懒加载**：不启动的插件**不 import**。列表 / 市场展示只用 `plugin.json` 元数据，
`plugin.py` 只在用户「启动」（或启用）时才被加载，不占内存。

**心跳与健康状态**：运行中的插件每 5 秒由管理器代写心跳日志；30 秒无心跳标记 🟡 无响应（可能卡死），
120 秒无心跳标记 🔴 卡死。设置页与开发者栏会显示健康圆点。

**崩溃恢复**：插件线程抛出未捕获异常（或 30s 无心跳）会被标记为「已崩溃」（crashed），
发送桌面通知；插件管理页显示崩溃原因，可「重启」；开启「插件崩溃后自动重启」（默认关）会自动重启。

**插件间通信（禁止直接 import / 调用函数）**：

```python
class Plugin(NonokaPlugin):
    def on_start(self):
        # 异步事件通知
        self.emit("my_plugin.ready", {"v": 1})
        # 同步服务调用（其它插件在 activate() 中 register_service 注册）
        svc = self.service("other_plugin_id")
        if svc is not None:
            svc.do_something()
```

---

## 3. 前端通信

插件前端通过 `iframe` 加载，用 `postMessage` 与 Python 通信：

```js
const PLUGIN = "MyPlugin";
function rpc(method, args) {
  return new Promise((resolve, reject) => {
    const callId = "c" + (++_seq);
    parent.postMessage({ __nonoka: true, plugin: PLUGIN, method, args, callId }, "*");
  });
}
rpc("do_something", [{ foo: 1 }]).then(res => console.log(res));
```

Python 侧推送进度/事件回 iframe：

```python
self._emit({"call_id": tid, "type": "progress", "percent": 50})
```

推送接口：`self.ctx.window.evaluate_js('window.NonokaShell.deliverToPlugin("MyPlugin", {...})')`。
前端在 `message` 事件中识别 `e.data.__nonoka_evt && e.data.plugin === PLUGIN` 后处理。

壳子向插件推送的通用事件：`{type:"state", state}`（运行状态变化）、`{type:"clipboard", url}`（剪贴板检测到链接）。

---

## 4. 任务队列（并行下载等）

后台耗时任务应提交到统一队列，自动获得并行数限制、暂停/恢复/取消能力：

```python
def do_something(self, payload):
    def runner(task):
        # task.is_cancelled() 可检查取消；task.set_progress(pct) 更新进度
        self._real_work(task, payload)
    return {"task_id": self.ctx.queue.submit(self.id, "download", "标题", payload, runner).id}
```

队列在「任务队列」页可见；第三方阻塞式核心无法真正中断，取消/暂停为尽力而为（已在 README 说明）。

---

## 5. 代理 / 通知 / 数据库

- **代理**：用户设置的代理（HTTP/SOCKS5 + 账号）由壳子统一管理，应用到 `requests` 与各下载核心；
  插件直接 `import requests` 即可自动走代理，无需自行处理。
- **桌面通知**：`self.ctx.notifier.notify(title, body)`（受用户开关控制）。
- **数据库**：下载成功后写入历史：`self.ctx.db.add_download(url, title, type, save_path)`。
- **配置**：按插件读写：`self.ctx.config.get_plugin(self.id)` / `self.ctx.config.set_plugin(self.id, key, val)`。

---

## 6. 元数据（plugin.json）

```json
{
  "id": "MyPlugin",
  "name": "我的插件",
  "version": "1.0.0",
  "config_version": 1,
  "core_version_min": "1.2.0",
  "repo": "your-github/your-repo",
  "author": "You",
  "description": "描述",
  "icon": "puzzle",
  "provides": ["service_a"],
  "consumes": ["ffmpeg_component"],
  "permissions": ["network_access", "file_write"],
  "sha256": "",
  "dependencies": [],
  "builtin": false,
  "system": false,
  "download": ""
}
```

- `repo`：用于插件自动更新与远程安装（比对 GitHub Release 版本）。
- `builtin: true`：随安装包分发、不可卸载。
- `system: true`：基础插件，不可卸载 / 禁用（只可查看与调整非致命参数）。
- `core_version_min`：要求的最低 Core 版本；不满足则标为「不兼容」不加载，管理页显示原因。
- `provides`：插件能提供的服务标识；`consumes`：插件需要的依赖标识。
- `permissions`：插件声明需要的权限（如 `network_access` / `file_write`）。
- `config_version`：插件配置版本号，用于配置迁移（见下节）。
- `sha256`：插件包校验值；插件市场安装/更新时下载 zip 后校验，不符则**拒绝安装**并提示「可能已被篡改」。
- 远程市场清单（Markets）为单独 JSON，每项同样可含 `sha256`；清单本身建议经 HTTPS 从可信仓库加载。

**插件数据目录**：插件私有数据统一放 `文档/NonokaLab/plugins_data/<plugin_id>/`
（可用 `self.ctx.plugin_manager.plugin_data_dir(self.id)` 获取）。卸载时用户可选择删除或保留。

---

## 6.1 配置迁移（config_version）

插件更新后若 `config_version` 升高，壳子会自动调用插件的迁移钩子（若实现）：

```python
class Plugin(NonokaPlugin):
    def migrate_config(self, old_version, new_version):
        # 把旧配置结构迁移到新结构；返回 True 表示成功
        cfg = self.ctx.config.get_plugin(self.id)
        if old_version == 1 and new_version == 2:
            cfg.setdefault("new_field", cfg.pop("old_field", None))
            for k, v in cfg.items():
                self.ctx.config.set_plugin(self.id, k, v)
        return True
```

未实现 `migrate_config` 时，壳子**保留旧配置文件**并在日志中警告，不会丢用户数据。

---

## 6.2 依赖管理与循环依赖

- 卸载插件前，壳子检查是否有其他插件 `consumes` 它：有则返回 `need_confirm` 列表，
  用户确认后才执行卸载。
- Core 扫描时对 `consumes` 做 DFS 环检测，发现循环依赖会记录错误并在列表标记，阻止异常加载。

---

## 7. 开发者模式与热重载

1. 设置页「开发者」开启「开发者模式」，壳子会扫描 `dev_plugins/` 并刷新插件列表。
2. 在开发者插件栏（或设置页开发者卡片）点「重新加载」即可热重载该插件（重新执行 `plugin.py` 模块体，
   不重启壳子）。
3. 点「查看运行日志」查看该插件相关日志（来自 `logs/nonoka.log`）。

---

## 8. 调试

- 日志：`用户文档/NonokaLab/logs/nonoka.log`。
- 崩溃报告：`用户文档/NonokaLab/logs/crash_report.json`。
- 从源码运行：`pip install -r requirements.txt && python -m nonoka_shell`（开发者模式下 `autostart` 写注册表会提示不支持，属正常）。
