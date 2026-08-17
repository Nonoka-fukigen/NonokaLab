/* ============================================================
   Nonoka Lab 外壳前端逻辑
   - 左侧导航（工具 = 插件；设置 / 关于 = 壳子页面）
   - 右侧内容：插件与壳子页面都通过 #contentFrame 加载
   - 插件与壳子页通过 postMessage 与 Python 通信
   - Python -> 前端 事件通过 window.NonokaShell.* 注入
   - 多语言：从 api.get_locales() 读取 zh/en 文案
   ============================================================ */
(function () {
  "use strict";

  var App = {
    brand: null,
    plugins: [],
    locales: { zh: {}, en: {} },
    locale: "zh",
    active: null,
    frameKind: null, // "plugin" | "page"
    theme: "auto",
    devMode: false,
  };

  function api() { return window.pywebview ? window.pywebview.api : null; }

  /* ---------------- 多语言 ---------------- */
  function T(key) {
    var d = App.locales[App.locale] || {};
    if (d[key] != null) return d[key];
    if (App.locales.zh[key] != null) return App.locales.zh[key];
    return key;
  }
  function tfmt(key, kv) {
    var s = T(key);
    if (kv) for (var k in kv) s = s.split("{" + k + "}").join(kv[k]);
    return s;
  }

  function icon(id, cls) {
    return '<svg class="icon ' + (cls || "") + '" aria-hidden="true"><use href="#' + id + '"/></svg>';
  }
  function el(html) {
    var t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstChild;
  }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* ---------------- 全局注入（Python 调用） ---------------- */
  window.NonokaShell = {
    deliverToPlugin: function (plugin, event) {
      var frame = document.getElementById("contentFrame");
      if (frame && frame.contentWindow) {
        frame.contentWindow.postMessage({ __nonoka_evt: true, plugin: plugin, event: event }, "*");
      }
    },
    showError: function (title, detail) { showModal(title || T("crash_title"), detail || "", null); },
    onComponentEvent: function (evt) { deliverToPage("onComponentEvent", evt); },
    onComponentsChanged: function (list) { deliverToPage("onComponentsChanged", list); },
    onPluginEvent: function (evt) { deliverToPage("onPluginEvent", evt); },
    onPluginsChanged: function (list) {
      try {
        App.plugins = list || [];
        App.pluginStates = App.pluginStates || {};
        (list || []).forEach(function (p) {
          if (p && p.id) App.pluginStates[p.id] = p.running;
        });
        refreshRunningBadges();
        buildNav();
        // 开发者模式可能被切换（设置页开关 / 关于页版本号彩蛋），同步刷新导航
        try {
          api().get_dev_mode().then(function (v) {
            var dv = !!v;
            if (dv !== App.devMode) { App.devMode = dv; buildNav(); }
          });
        } catch (e2) {}
      } catch (e) {}
      deliverToPage("onPluginsChanged", list);
    },
    onUpdateEvent: function (info) { onUpdateEvent(info); },
    onCrash: function (info) { onCrash(info); },
    onWelcome: function (info) { onWelcome(info); },
    /* 托盘「设置...」菜单：弹窗并跳到设置页 */
    onSettingsRequested: function () { try { selectShell("settings"); } catch (e) {} },
    onTheme: function (info) { applyTheme(info && info.mode); },
    onPluginState: function (info) {
      if (info && info.id) {
        App.pluginStates = App.pluginStates || {};
        App.pluginStates[info.id] = info.state;
        refreshRunningBadges();
        window.NonokaShell.deliverToPlugin(info.id, { type: "state", state: info.state });
      }
      deliverToPage("onPluginState", info);
    },
    onClipboardDetect: function (info) { onClipboardDetect(info); },
    /* 控制台：Python 新日志实时推送到控制台页面 */
    onConsoleLog: function (records) { deliverToPage("onConsoleLog", records || []); },
    /* 开发者模式变化后刷新导航（about 页版本号彩蛋 / 设置页开关调用） */
    refreshNav: function () {
      try {
        return api() && api().get_dev_mode().then(function (v) {
          App.devMode = !!v;
          buildNav();
        });
      } catch (e) { return null; }
    },
    setDevMode: function (v) {
      try {
        return api() && api().set_dev_mode(!!v).then(function () {
          App.devMode = !!v;
          buildNav();
        });
      } catch (e) { return null; }
    },
  };

  /* ---------------- 主题 ---------------- */
  function applyTheme(mode) {
    if (!mode) mode = "auto";
    App.theme = mode;
    document.documentElement.setAttribute("data-theme", mode);
    applyThemeToFrame();
  }
  function applyThemeToFrame() {
    try {
      var f = document.getElementById("contentFrame");
      if (f && f.contentDocument && f.contentDocument.documentElement) {
        f.contentDocument.documentElement.setAttribute("data-theme", App.theme || "auto");
      }
    } catch (e) {}
  }

  /* ---------------- 剪贴板检测 ---------------- */
  function onClipboardDetect(info) {
    if (!info || !info.url) return;
    // 通知插件前端预填链接
    window.NonokaShell.deliverToPlugin(App.plugins[0] ? App.plugins[0].id : "Nonoka_video_download",
      { type: "clipboard", url: info.url });
    showModal(T("clipboard_detected"), info.url,
      [{
        label: T("clipboard_jump"), onClick: function () {
          if (App.plugins[0]) selectPlugin(App.plugins[0]);
        }
      },
      { label: T("ok"), cls: "btn gray" }]);
  }

  function deliverToPage(fn, payload) {
    var frame = document.getElementById("contentFrame");
    if (frame && frame.contentWindow) {
      frame.contentWindow.postMessage({ __nonoka_page: true, fn: fn, payload: payload }, "*");
    }
  }

  /* ---------------- iframe <-> Python 中继（插件调用） ---------------- */
  window.addEventListener("message", function (e) {
    var d = e.data;
    if (!d) return;
    // 开发者模式切换后刷新导航（about 页版本号彩蛋等）
    if (d.__nonoka_nav_refresh) {
      try { window.NonokaShell.refreshNav(); } catch (e2) {}
      return;
    }
    // 壳子页面 postMessage 中继调用 Python（file:// 下 iframe 无法直接访问 parent.pywebview.api）
    if (d.__nonoka_page_call) {
      var pa = api();
      if (!pa || typeof pa[d.method] !== "function") {
        safePost(e.source, { __nonoka_page_resp: true, callId: d.callId, error: "no method: " + d.method });
        return;
      }
      try {
        var pres = pa[d.method].apply(pa, d.args || []);
        Promise.resolve(pres).then(function (r) {
          safePost(e.source, { __nonoka_page_resp: true, callId: d.callId, result: r });
        }).catch(function (err) {
          safePost(e.source, { __nonoka_page_resp: true, callId: d.callId, error: String(err && err.message ? err.message : err) });
        });
      } catch (err) {
        safePost(e.source, { __nonoka_page_resp: true, callId: d.callId, error: String(err) });
      }
      return;
    }
    if (!d.__nonoka) return;
    var a = api();
    var target = (a && a[d.plugin]) ? a[d.plugin] : null;
    var fn = target ? target[d.method] : null;
    if (typeof fn !== "function" && a && typeof a.invoke_plugin !== "function") {
      safePost(e.source, { __nonoka_resp: true, callId: d.callId, error: "no method: " + d.plugin + "." + d.method });
      return;
    }
    var reply;
    if (typeof fn === "function") {
      try {
        reply = fn.apply(target, d.args || []);
      } catch (err) {
        safePost(e.source, { __nonoka_resp: true, callId: d.callId, error: String(err) });
        return;
      }
    } else {
      // pywebview 嵌套 js_api 方法不可靠（bridge 可能捕获到空代理 / 未暴露嵌套方法），
      // 回退到顶层 invoke_plugin(pid, method, args) 统一派发，保证插件 RPC 稳定可调。
      try {
        reply = a.invoke_plugin(d.plugin, d.method, d.args || []);
      } catch (err) {
        safePost(e.source, { __nonoka_resp: true, callId: d.callId, error: String(err) });
        return;
      }
    }
    Promise.resolve(reply).then(function (r) {
      safePost(e.source, { __nonoka_resp: true, callId: d.callId, result: r });
    }).catch(function (err) {
      safePost(e.source, { __nonoka_resp: true, callId: d.callId, error: String(err && err.message ? err.message : err) });
    });
  });

  function safePost(win, obj) {
    try { win.postMessage(obj, "*"); } catch (e) {}
  }

  /* ---------------- 启动 ---------------- */
  function init() {
    var frame = document.getElementById("contentFrame");
    if (frame) {
      frame.addEventListener("load", function () {
        applyThemeToFrame();
      });
    }
    Promise.resolve(api().get_brand()).then(function (b) {
      App.brand = b;
      applyBrand(b);
      document.getElementById("brandName").textContent = b.app_name;
      document.getElementById("sideFoot").textContent = "v" + b.version + " · " + b.publisher;
    });
    Promise.resolve(api().get_locales()).then(function (loc) {
      if (loc) { App.locales.zh = loc.zh || {}; App.locales.en = loc.en || {}; }
      return api().get_locale();
    }).then(function (lc) {
      App.locale = lc || "zh";
      return api().get_dev_mode();
    }).then(function (dev) {
      App.devMode = !!dev;
      buildNav();
      return api().get_theme();
    }).then(function (mode) {
      applyTheme(mode || "auto");
      return api().get_plugins();
    }).then(function (list) {
      App.plugins = list || [];
      App.pluginStates = {};
      (App.plugins || []).forEach(function (p) { App.pluginStates[p.id] = p.running; });
      refreshRunningBadges();
      buildNav();
      if (visiblePlugins().length) selectPlugin(visiblePlugins()[0]);
      else selectShell("settings");
    }).catch(function () { buildNav(); });
  }

  function applyBrand(b) {
    var r = document.documentElement.style;
    if (b.theme_primary) r.setProperty("--accent", b.theme_primary);
    if (b.theme_primary_dark) r.setProperty("--accent-press", b.theme_primary_dark);
    if (b.theme_primary_soft) r.setProperty("--accent-soft", b.theme_primary_soft);
  }

  /* 默认模式只显示「视频下载」插件；开发者模式显示全部 */
  function visiblePlugins() {
    var dev = !!App.devMode;
    return (App.plugins || []).filter(function (p) {
      if (p.incompatible && !dev) return false;
      if (dev) return true;
      return p.id === "Nonoka_video_download";
    });
  }

  /* ---------------- 导航 ---------------- */
  function buildNav() {
    var nav = document.getElementById("nav");
    nav.innerHTML = "";
    var dev = !!App.devMode;
    var plugs = visiblePlugins();

    if (plugs.length) {
      nav.appendChild(el('<div class="nav-group-title">' + T("nav_tools") + "</div>"));
      plugs.forEach(function (p) {
        var item = el('<div class="nav-item" data-kind="plugin" data-id="' + p.id + '">' +
          icon(p.icon || "square") + "<span>" + esc(p.name) + "</span>" +
          (p.incompatible ? '<span class="tag no" style="font-size:10px;margin-left:4px">' + T("incompatible") + "</span>" : "") +
          '<span class="plug-switch" data-pid="' + p.id + '" role="switch" aria-checked="false" title="' + T("plug_toggle") + '">' +
          '<span class="knob"></span></span></div>');
        var sw = item.querySelector(".plug-switch");
        if (sw) sw.addEventListener("click", function (e) { e.stopPropagation(); togglePlugin(p); });
        item.addEventListener("click", function () { selectPlugin(p); });
        nav.appendChild(item);
      });
    }

    if (dev) {
      nav.appendChild(el('<div class="nav-group-title">' + T("nav_data") + "</div>"));
      [["history", T("nav_history"), "clock"], ["queue", T("nav_queue"), "list"]].forEach(function (s) {
        var item = el('<div class="nav-item" data-kind="shell" data-id="' + s[0] + '">' +
          icon(s[2]) + "<span>" + s[1] + "</span></div>");
        item.addEventListener("click", function () { selectShell(s[0]); });
        nav.appendChild(item);
      });

      nav.appendChild(el('<div class="nav-group-title">' + T("nav_dev") + "</div>"));
      [["developer", T("nav_dev"), "code"]].forEach(function (s) {
        var item = el('<div class="nav-item" data-kind="shell" data-id="' + s[0] + '">' +
          icon(s[2]) + "<span>" + s[1] + "</span></div>");
        item.addEventListener("click", function () { selectShell(s[0]); });
        nav.appendChild(item);
      });
    }

    nav.appendChild(el('<div class="nav-group-title">' + T("nav_settings") + "</div>"));
    [["settings", T("nav_settings"), "gear"], ["console", T("nav_console"), "code"], ["debug", T("nav_debug"), "eye"], ["about", T("nav_about"), "info"]].forEach(function (s) {
      var item = el('<div class="nav-item" data-kind="shell" data-id="' + s[0] + '">' +
        icon(s[2]) + "<span>" + s[1] + "</span></div>");
      item.addEventListener("click", function () { selectShell(s[0]); });
      nav.appendChild(item);
    });

    refreshRunningBadges();
    // 重建后恢复当前选中项（启动/停止等触发的重建不应丢失选中高亮）
    if (App.active) setActive(App.active);
  }

  function refreshRunningBadges() {
    App.plugins.forEach(function (p) {
      var st = (App.pluginStates && App.pluginStates[p.id]) || p.running;
      // 同步左侧导航的苹果式滑动开关状态
      var sw = document.querySelector('.plug-switch[data-pid="' + p.id + '"]');
      if (sw) {
        var on = st === "running" || st === "paused";
        sw.classList.toggle("on", on);
        sw.classList.toggle("paused", st === "paused");
        sw.setAttribute("aria-checked", on ? "true" : "false");
        sw.title = st === "running" ? T("plugin_stop") : (st === "paused" ? T("plugin_resume") : T("plugin_start"));
      }
    });
  }

  /* 左侧导航开关：按当前状态启动 / 停止 / 继续插件 */
  function togglePlugin(p) {
    var a = api();
    if (!a) return;
    var st = (App.pluginStates && App.pluginStates[p.id]) || p.running;
    if (st === "running") {
      Promise.resolve(a.stop_plugin(p.id)).catch(function () {});
    } else if (st === "paused") {
      Promise.resolve(a.resume_plugin(p.id)).catch(function () {});
    } else {
      Promise.resolve(a.start_plugin(p.id)).catch(function () {});
    }
  }

  function setActive(id) {
    App.active = id;
    document.querySelectorAll(".nav-item").forEach(function (n) {
      n.classList.toggle("active", n.getAttribute("data-id") === id);
    });
  }

  function selectPlugin(p) {
    setActive(p.id);
    App.frameKind = "plugin";
    document.getElementById("topTitle").textContent = p.name;
    var frame = document.getElementById("contentFrame");
    if (frame.getAttribute("data-src") !== p.frontend) {
      frame.setAttribute("data-src", p.frontend);
      frame.src = p.frontend;
    }
  }

  function selectShell(id) {
    setActive(id);
    App.frameKind = "page";
    var frame = document.getElementById("contentFrame");
    var url = "pages/" + id + ".html";
    var titles = {
      settings: T("settings_title"), about: T("about_title"),
      history: T("history_title"), queue: T("nav_queue"), developer: T("nav_dev"),
      console: T("nav_console"), debug: T("debug_title"),
    };
    document.getElementById("topTitle").textContent = titles[id] || T("settings_title");
    if (frame.getAttribute("data-src") !== url) {
      frame.setAttribute("data-src", url);
      frame.src = url;
    } else {
      applyThemeToFrame();
    }
  }

  /* ---------------- 更新事件 ---------------- */
  function onUpdateEvent(info) {
    if (!info) return;
    if (info.startup && info.update_available) {
      showModal(tfmt("new_version", { latest: info.latest }),
        tfmt("new_version_body", { current: info.current, notes: info.notes || "" }),
        [{ label: T("ok"), href: info.url }]);
      return;
    }
    if (info.done && info.ok) {
      showModal(T("update_downloaded"),
        tfmt("update_downloaded_body", { path: info.path || "" }),
        [{ label: T("open_log") || "打开", onClick: function () { api() && api().open_external_folder(info.path); } }]);
      return;
    }
    // 进度 / 失败：转发给设置页（如有）
    deliverToPage("onUpdateEvent", info);
  }

  /* ---------------- 崩溃弹窗（隐私：默认不含日志，用户勾选才包含） ---------------- */
  function onCrash(info) {
    showModal(T("crash_title"), T("crash_body"),
      [
        { label: T("crash_submit"), onClick: function () { api() && api().open_crash_issue(false); } },
        { label: T("include_logs"), onClick: function () { api() && api().open_crash_issue(true); } },
        { label: T("crash_cancel"), cls: "btn gray" },
      ]);
  }

  /* ---------------- 首次引导 ---------------- */
  function onWelcome(info) {
    if (!info || !info.show) return;
    var mask = document.getElementById("welcomeMask");
    document.getElementById("welcomeTitle").textContent = T("welcome_title");
    document.getElementById("welcomeDesc").textContent = T("welcome_desc");
    var hint = document.getElementById("welcomeHint");
    if (info.ffmpeg_missing) {
      hint.style.display = "block";
      hint.textContent = "⚠ " + T("welcome_ffmpeg_hint");
    } else {
      hint.style.display = "none";
    }
    mask.classList.add("show");
  }

  function dismissWelcome() {
    document.getElementById("welcomeMask").classList.remove("show");
    api() && api().set_welcome_done(true);
  }

  /* ---------------- 弹窗 ---------------- */
  function showModal(title, detail, actions) {
    var mask = document.getElementById("modalMask");
    document.getElementById("modalTitle").textContent = title;
    document.getElementById("modalBody").textContent = detail || "";
    var acts = document.getElementById("modalActs");
    acts.innerHTML = "";
    var close = el('<button class="btn gray">' + T("ok") + "</button>");
    close.addEventListener("click", function () { mask.classList.remove("show"); });
    acts.appendChild(close);
    (actions || []).forEach(function (a) {
      if (a.href) {
        var b = el('<a class="btn" href="' + a.href + '" target="_blank" rel="noopener">' + esc(a.label) + "</a>");
        acts.appendChild(b);
      } else {
        var c = el('<button class="' + (a.cls || "btn") + '">' + esc(a.label) + "</button>");
        c.addEventListener("click", function () {
          if (a.onClick) a.onClick();
          mask.classList.remove("show");
        });
        acts.appendChild(c);
      }
    });
    mask.classList.add("show");
  }

  /* ---------------- 入口 ---------------- */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else { boot(); }

  function boot() {
    document.getElementById("modalMask").addEventListener("click", function (e) {
      if (e.target === this) this.classList.remove("show");
    });
    document.getElementById("welcomeStart").addEventListener("click", dismissWelcome);
    document.getElementById("welcomeSettings").addEventListener("click", function () {
      dismissWelcome(); selectShell("settings");
    });
    if (window.pywebview && window.pywebview.api) {
      init();
    } else {
      window.addEventListener("pywebviewready", init, { once: true });
    }
  }
})();
