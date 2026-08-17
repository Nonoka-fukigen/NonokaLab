# -*- coding: utf-8 -*-
"""同步服务定位器。

- 插件在 activate() 中注册自己提供的服务：core.register_service(plugin_id, service)
- 其它插件通过 core.get_service(plugin_id) 同步获取并调用
- 禁止插件之间直接 import 或直接调用函数（唯一通道：EventBus 异步 / ServiceLocator 同步）
"""
import threading


class ServiceLocator:
    """同步服务注册表：plugin_id -> service 对象。"""

    def __init__(self):
        self._services = {}
        self._lock = threading.Lock()

    def register(self, plugin_id, service):
        with self._lock:
            self._services[plugin_id] = service

    def unregister(self, plugin_id):
        with self._lock:
            self._services.pop(plugin_id, None)

    def get(self, plugin_id):
        """同步获取服务（可能为 None）。"""
        with self._lock:
            return self._services.get(plugin_id)

    def has(self, plugin_id):
        with self._lock:
            return plugin_id in self._services

    def call(self, plugin_id, method, *args, **kwargs):
        """同步调用服务方法；未注册则抛 KeyError。"""
        svc = self.get(plugin_id)
        if svc is None:
            raise KeyError("服务未注册: %s" % plugin_id)
        return getattr(svc, method)(*args, **kwargs)

    def services(self):
        with self._lock:
            return list(self._services.keys())
