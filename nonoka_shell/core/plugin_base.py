# -*- coding: utf-8 -*-
"""插件基类：所有插件（含基础插件、功能插件）的唯一契约。

插件之间禁止直接 import 或直接调用函数：
- 异步事件通知 → core.EventBus（emit / on）
- 同步服务调用 → core.ServiceLocator（register / get）
"""
import logging

log = logging.getLogger("nonoka.plugin")


class Plugin:
    """所有插件的唯一契约。"""

    plugin_id = "base"
    name = ""

    def __init__(self, core=None):
        self.core = core

    def activate(self, core):
        """用户手动启动插件时调用（懒加载：此刻才 import）。可在此注册服务。"""
        pass

    def deactivate(self):
        """停止 / Core 退出时调用。释放线程、文件句柄等资源。"""
        pass

    def migrate_config(self, old_version, new_version):
        """config_version 变化时被调用；返回 True 表示迁移成功。"""
        return False

    # ---------------- 便捷工具 ----------------
    def service(self, plugin_id):
        """通过服务定位器获取其它插件提供的服务（禁止直接 import）。"""
        if self.core is None:
            return None
        return self.core.get_service(plugin_id)

    def emit(self, topic, data=None):
        if self.core is None:
            return
        self.core.bus.emit(topic, data)
