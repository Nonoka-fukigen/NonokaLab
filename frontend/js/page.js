/* ============================================================
   Nonoka Lab —— 壳子页面共享逻辑（settings.html / about.html 引用）
   - 通过 window.parent.pywebview.api 调用 Python
   - 监听来自 shell 的页面事件 {__nonoka_page:true, fn, payload}
   - 多语言 T() 来自父窗口的语言包
   - 轻量模态框
   ============================================================ */
(function () {
  "use strict";

  var App = { locales: { zh: {}, en: {} }, locale: "zh" };

  /* ---- 跨源兜底：经外壳 postMessage 中继调用 Python ----
     file:// 下 iframe 属不透明源，无法直接访问 window.parent.pywebview.api，
     因此当同源直连不可用时，改为发给外壳中继。 */
  var _seq = 0;
  var _pending = {};
  var RPC_TIMEOUT = 4000; // 跨源中继最长等待，超时兜底避免页面空白
  window.addEventListener("message", function (e) {
    var d = e.data;
    if (d && d.__nonoka_page_resp && _pending[d.callId]) {
      var p = _pending[d.callId]; delete _pending[d.callId];
      if (d.error) p.reject(new Error(d.error)); else p.resolve(d.result);
    }
  });
  function pageRpcProxy() {
    return new Proxy({}, {
      get: function (t, prop) {
        if (typeof prop !== "string") return undefined;
        return function () {
          var args = Array.prototype.slice.call(arguments);
          return new Promise(function (resolve, reject) {
            var callId = "p" + (++_seq);
            _pending[callId] = { resolve: resolve, reject: reject };
            try {
              (window.parent || window).postMessage(
                { __nonoka_page_call: true, method: prop, args: args, callId: callId }, "*");
            } catch (err) { delete _pending[callId]; reject(err); return; }
            // 超时兜底：外壳未及时响应时也结束等待，避免页面卡在空白
            setTimeout(function () {
              if (_pending[callId]) {
                var p = _pending[callId]; delete _pending[callId];
                reject(new Error("rpc timeout: " + String(prop)));
              }
            }, RPC_TIMEOUT);
          });
        };
      }
    });
  }
  function api() {
    try {
      if (window.parent && window.parent.pywebview && window.parent.pywebview.api)
        return window.parent.pywebview.api;
    } catch (e) {}
    try {
      if (window.pywebview && window.pywebview.api) return window.pywebview.api;
    } catch (e) {}
    return pageRpcProxy();
  }
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

  var handlers = {};
  window.addEventListener("message", function (e) {
    var d = e.data;
    if (!d || !d.__nonoka_page) return;
    // 主题统一应用（无需各页面单独注册）
    if (d.fn === "onTheme" && d.payload && d.payload.mode) {
      document.documentElement.setAttribute("data-theme", d.payload.mode);
    }
    var h = handlers[d.fn];
    if (h) h(d.payload);
  });
  function onPage(fn, cb) { handlers[fn] = cb; }

  function boot(cb) {
    window.__pageBoot = { called: true, done: false, error: null };
    var cb1 = function (err) {
      window.__pageBoot.done = true;
      window.__pageBoot.error = err ? String(err) : null;
      try { cb && cb(); } catch (e) { window.__pageBoot.error = "render-exc:" + String(e); }
    };
    var a = api();
    if (!a) { cb1(); return; }
    try {
      Promise.resolve(a.get_locales())
        .then(function (loc) {
          if (loc) { App.locales.zh = loc.zh || {}; App.locales.en = loc.en || {}; }
          return a.get_locale();
        })
        .then(function (lc) { App.locale = lc || "zh"; cb1(); })
        .catch(function (e) { cb1(e); });
    } catch (e) { cb1(e); }
  }

  /* 轻量模态 */
  function modal(title, body, actions) {
    var mask = el('<div class="modal-mask show"><div class="modal"><h3></h3><p></p><div class="acts"></div></div></div>');
    mask.querySelector("h3").textContent = title;
    mask.querySelector("p").textContent = body || "";
    var acts = mask.querySelector(".acts");
    var close = el('<button class="btn gray">' + T("ok") + "</button>");
    close.addEventListener("click", function () { document.body.removeChild(mask); });
    acts.appendChild(close);
    (actions || []).forEach(function (a) {
      if (a.href) {
        acts.appendChild(el('<a class="btn" href="' + a.href + '" target="_blank" rel="noopener">' + esc(a.label) + "</a>"));
      } else {
        var b = el('<button class="' + (a.cls || "btn") + '">' + esc(a.label) + "</button>");
        b.addEventListener("click", function () {
          if (a.onClick) a.onClick();
          try { document.body.removeChild(mask); } catch (e) {}
        });
        acts.appendChild(b);
      }
    });
    mask.addEventListener("click", function (e) { if (e.target === mask) document.body.removeChild(mask); });
    document.body.appendChild(mask);
  }

  window.Page = {
    api: api, T: T, tfmt: tfmt, icon: icon, el: el, esc: esc,
    onPage: onPage, boot: boot, modal: modal,
  };
})();
