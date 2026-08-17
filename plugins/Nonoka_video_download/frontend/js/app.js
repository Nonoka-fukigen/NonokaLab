/* ============================================================
   Nonoka_video_download 插件前端（重写版）
   - 粘贴链接即自动识别平台（B站/抖音），无需手动「解析」
   - 点「开始下载」直接一体化：自动识别 → 解析 → 下载
   - 「下载视频 / 下载封面」两标签
   ============================================================ */
(function () {
  "use strict";

  var PLUGIN = "Nonoka_video_download";

  /* 平台列表：手动选择（用户主权）；detect 仅作链接识别辅助显示 */
  var PLATFORMS = [
    { id: "bilibili", name: "哔哩哔哩", icon: "bilibili" },
    { id: "douyin", name: "抖音", icon: "douyin" },
  ];

  var state = {
    platform: "bilibili", mode: "both", tab: "video",
    detect: null,            // 链接识别结果（辅助显示，不覆盖用户选择）
    taskId: null, ffmpeg: null, ffmpegIgnored: false,
    running: false,
  };

  /* -------- 通信 -------- */
  var _seq = 0;
  var _pending = {};
  var RPC_TIMEOUT = 4000;
  function postRpc(callId, method, args, msg, timeout) {
    _pending[callId] = { method: method };
    try {
      parent.postMessage(msg, "*");
    } catch (err) { delete _pending[callId]; throw err; }
    setTimeout(function () {
      if (_pending[callId]) {
        var p = _pending[callId]; delete _pending[callId];
        if (p.reject) p.reject(new Error("rpc timeout: " + String(method)));
      }
    }, timeout || RPC_TIMEOUT);
  }
  function waitRpc(callId) {
    return new Promise(function (resolve, reject) {
      _pending[callId].resolve = resolve;
      _pending[callId].reject = reject;
    });
  }
  function rpc(method, args, timeout) {
    var callId = "c" + (++_seq);
    try {
      postRpc(callId, method, args, { __nonoka: true, plugin: PLUGIN, method: method, args: args || [], callId: callId }, timeout);
    } catch (err) { return Promise.reject(err); }
    return waitRpc(callId);
  }
  window.addEventListener("message", function (e) {
    var d = e.data;
    if (d && (d.__nonoka_resp || d.__nonoka_page_resp) && _pending[d.callId] && _pending[d.callId].resolve) {
      var p = _pending[d.callId]; delete _pending[d.callId];
      if (d.error) p.reject(new Error(d.error)); else p.resolve(d.result);
    }
    if (d && d.__nonoka_evt && d.plugin === PLUGIN) handleEvent(d.event);
  });

  /* -------- 运行状态 -------- */
  function setRunState(st) {
    var badge = $("runStatus");
    if (!badge) return;
    state.running = (st === "running");
    if (st === "running") { badge.className = "tag run"; badge.textContent = "运行中"; }
    else if (st === "paused") { badge.className = "tag wait"; badge.textContent = "已暂停"; }
    else { badge.className = "tag stop"; badge.textContent = "已停止"; }
    applyRunningUI();
  }
  function applyRunningUI() {
    var wrap = document.querySelector(".wrap");
    if (wrap) wrap.classList.toggle("locked", !state.running);
    var main = $("btnMain");
    if (main) main.disabled = !state.running;
    var a = $("runHint");
    if (a) {
      a.textContent = state.running
        ? "插件运行中，可正常使用下载功能"
        : "插件未运行，功能已禁用，请使用左侧导航栏的开关按钮启动插件";
    }
  }
  function requireRunning() {
    if (state.running) return true;
    showModal("需要启动插件", "该功能需要在插件运行后才能使用。请使用左侧导航栏的开关按钮启动插件。");
    return false;
  }
  function refreshRunState() {
    var a = shell();
    if (!a || !a.get_plugin_state) return;
    Promise.resolve(a.get_plugin_state(PLUGIN)).then(setRunState).catch(function () {});
  }

  /* -------- DOM / 通信辅助 -------- */
  function $(id) { return document.getElementById(id); }
  function icon(id) { return '<svg class="icon" aria-hidden="true"><use href="#' + id + '"/></svg>'; }
  function el(html) { var t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }
  function shell() {
    try {
      if (window.parent && window.parent.pywebview && window.parent.pywebview.api)
        return window.parent.pywebview.api;
    } catch (e) {}
    return new Proxy({}, {
      get: function (t, prop) {
        if (typeof prop !== "string") return undefined;
        return function () {
          var args = Array.prototype.slice.call(arguments);
          var callId = "p" + (++_seq);
          try {
            postRpc(callId, prop, args, { __nonoka_page_call: true, method: prop, args: args, callId: callId });
          } catch (err) { return Promise.reject(err); }
          return waitRpc(callId);
        };
      }
    });
  }

  /* -------- 事件处理（Python 推送） -------- */
  function handleEvent(ev) {
    if (!ev) return;
    switch (ev.type) {
      case "progress":
        setStatus((ev.label || "") + (ev.percent != null ? " " + ev.percent + "%" : ""), "wait");
        progBar().style.width = (ev.percent || 0) + "%";
        break;
      case "log":
        log(ev.message);
        break;
      case "title":
        if (ev.title) $("fileName") && ($("fileName").value = ev.title);
        break;
      case "done":
        if (ev.ok) {
          setStatus("✅ 下载完成", "ok");
          progBar().style.width = "100%";
          $("btnOpen").style.display = "";
        } else {
          setStatus("❌ 失败：" + ev.message, "no");
          log("失败：" + ev.message);
          showModal("下载失败", ev.message);
        }
        enableActions(true);
        break;
      case "state":
        setRunState(ev.state);
        break;
      case "qr_success":
        if (ev.cookie) $("cookie").value = ev.cookie;
        hideMask("qrMask");
        setStatus("已通过扫码登录", "ok");
        break;
      case "qr_expired":
        hideMask("qrMask");
        showModal("二维码已过期", "请重新点击「扫码登录」获取新二维码。");
        break;
      case "qr_error":
        hideMask("qrMask");
        showModal("扫码失败", ev.message || "未知错误");
        break;
      case "clipboard":
        if (ev.url) {
          $("url").value = ev.url;
          detectUrl();
          setStatus("已填入剪贴板链接，可直接下载", "wait");
        }
        break;
    }
  }

  /* -------- UI 辅助 -------- */
  function progBar() { return document.querySelector("#prog > i"); }
  function setStatus(text, kind) {
    var s = $("status");
    s.textContent = text;
    s.className = "tag " + (kind || "wait");
  }
  function log(m) {
    var b = $("logbox");
    b.textContent += m + "\n";
    b.scrollTop = b.scrollHeight;
  }
  function enableActions(on) {
    $("btnMain").disabled = !on;
  }

  /* -------- 自动识别（辅助显示，不覆盖用户选择的平台） -------- */
  function detectPlatform(raw) {
    var s = String(raw || "").trim().toLowerCase();
    if (!s) return null;
    if (s.indexOf("bilibili.com") >= 0 || s.indexOf("b23.tv") >= 0 ||
        s.indexOf("bv") === 0 || /(^|[^a-z0-9])bv[0-9a-z]{8,}/.test(s)) return "bilibili";
    if (s.indexOf("douyin.com") >= 0 || s.indexOf("iesdouyin") >= 0 ||
        s.indexOf("抖音") >= 0 || s.indexOf("打开抖音") >= 0) return "douyin";
    return null;
  }
  function detectUrl() {
    var raw = $("url").value;
    var p = detectPlatform(raw);
    var badge = $("urlDetect");
    state.detect = p;
    if (!p) {
      badge.className = "tag wait";
      badge.textContent = raw.trim() ? "未识别（仍可下载）" : "待粘贴链接";
    } else if (p === "bilibili") {
      badge.className = "tag ok";
      badge.innerHTML = icon("bilibili") + " 哔哩哔哩";
    } else {
      badge.className = "tag ok";
      badge.innerHTML = icon("douyin") + " 抖音";
    }
    // 如果识别结果与当前平台不同，提示用户可切换（不强制）
    if (p && p !== state.platform) {
      badge.title = "识别为 " + (p === "bilibili" ? "哔哩哔哩" : "抖音") + "，当前选 " +
        (state.platform === "bilibili" ? "哔哩哔哩" : "抖音") + "（点击平台按钮可切换）";
    } else {
      badge.title = "";
    }
  }

  /* -------- 平台选择（手动） -------- */
  function renderPlatform() {
    var host = $("platformSeg");
    host.innerHTML = "";
    PLATFORMS.forEach(function (p) {
      var b = el('<button data-p="' + p.id + '">' + icon(p.icon || "video") + p.name + "</button>");
      if (p.id === state.platform) b.classList.add("on");
      b.addEventListener("click", function () { if (!requireRunning()) return; setPlatform(p.id); });
      host.appendChild(b);
    });
  }
  function setPlatform(p) {
    state.platform = p;
    document.querySelectorAll("#platformSeg button").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-p") === p);
    });
    // B站专属字段：清晰度、Cookie
    $("qualityField").style.display = p === "bilibili" ? "" : "none";
    $("cookieField").style.display = p === "bilibili" ? "" : "none";
    rpc("set_config", ["platform", p]).catch(function () {});
    refreshFfmpegWarn();
    detectUrl();
  }

  /* -------- 模式 & FFmpeg -------- */
  function setMode(m) {
    state.mode = m;
    document.querySelectorAll("#modeSeg button").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-m") === m);
    });
    rpc("set_config", ["mode", m]).catch(function () {});
    refreshFfmpegWarn();
  }
  function refreshFfmpegWarn() {
    var warn = $("ffWarn");
    if (state.mode === "both" && state.ffmpeg && !state.ffmpeg.installed) warn.style.display = "";
    else warn.style.display = "none";
  }
  function refreshFfmpegBanner() {
    var b = $("ffmpegBanner");
    if (!b) return;
    var installed = !!(state.ffmpeg && state.ffmpeg.installed);
    b.style.display = (!installed && !state.ffmpegIgnored) ? "flex" : "none";
  }

  /* -------- 标签切换 -------- */
  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll(".tab-seg button").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-tab") === tab);
    });
    $("paneVideo").style.display = tab === "video" ? "" : "none";
    $("paneCover").style.display = tab === "cover" ? "" : "none";
    var main = $("btnMain");
    main.textContent = tab === "video" ? "⬇ 开始下载" : "下载封面";
    main.dataset.tab = tab;
  }

  /* -------- 操作：直接下载（自动识别平台，无需解析） -------- */
  function collectPayload() {
    return {
      url: $("url").value.trim(),
      platform: state.platform,
      mode: state.mode,
      folder: $("folder").value.trim() || "",
      filename: ($("fileName") && $("fileName").value.trim()) || "",
      quality: $("quality").value,
      cookie: $("cookie").value.trim() || "",
      scale: parseInt($("scale").value, 10) || 2,
    };
  }
  function doDownload() {
    if (!requireRunning()) return;
    var url = $("url").value.trim();
    if (!url) { setStatus("请先粘贴视频链接", "no"); return; }
    enableActions(false);
    progBar().style.width = "0%";
    $("btnOpen").style.display = "none";
    log("开始下载…（平台：" + (state.platform === "bilibili" ? "哔哩哔哩" : "抖音") + "）");
    rpc("download", [collectPayload()]).catch(function (e) {
      setStatus("下载异常：" + e.message, "no"); enableActions(true);
    });
  }
  function doCover() {
    if (!requireRunning()) return;
    var url = $("url").value.trim();
    if (!url) { setStatus("请先粘贴视频链接", "no"); return; }
    enableActions(false);
    log("开始下载封面…");
    rpc("cover", [collectPayload()]).catch(function (e) {
      setStatus("封面异常：" + e.message, "no"); enableActions(true);
    });
  }

  function pickFolder() {
    rpc("pick_folder", [], 60000).then(function (res) {
      var path = (typeof res === "object" && res !== null && res.data !== undefined) ? res.data : res;
      if (path) {
        document.getElementById("folder").value = path;
        rpc("set_config", ["last_folder", path]);
      }
    }).catch(function () {});
  }
  function openFolder() {
    var folder = $("folder").value.trim();
    if (!folder) { setStatus("请先设置保存文件夹", "no"); return; }
    rpc("open_folder", [folder]).catch(function () {});
  }

  /* -------- 扫码登录 -------- */
  function qrLogin() {
    if (!requireRunning()) return;
    rpc("qr_generate").then(function (r) {
      r = r && r.data ? r.data : r;
      if (r.error) { showModal("扫码登录失败", r.error); return; }
      $("qrImg").src = r.image;
      showMask("qrMask");
    }).catch(function (e) { showModal("扫码登录失败", String(e)); });
  }

  /* -------- 弹窗 -------- */
  function showModal(title, detail) {
    $("mTitle").textContent = title;
    $("mBody").textContent = detail || "";
    showMask("mMask");
  }
  function showMask(id) { $(id).classList.add("show"); }
  function hideMask(id) { $(id).classList.remove("show"); }

  /* -------- 初始化 -------- */
  function init() {
    renderPlatform();
    document.querySelectorAll(".tab-seg button").forEach(function (b) {
      b.addEventListener("click", function () { if (!requireRunning()) return; setTab(b.getAttribute("data-tab")); });
    });
    document.querySelectorAll("#modeSeg button").forEach(function (b) {
      b.addEventListener("click", function () { if (!requireRunning()) return; setMode(b.getAttribute("data-m")); });
    });
    // 粘贴/输入即识别（辅助）
    $("url").addEventListener("input", function () { detectUrl(); });
    $("btnMain").addEventListener("click", function () {
      if (state.tab === "video") doDownload(); else doCover();
    });
    $("btnBrowse").addEventListener("click", pickFolder);
    $("btnOpen").addEventListener("click", openFolder);
    $("btnQr").addEventListener("click", qrLogin);
    $("btnCookieHelp").addEventListener("click", function () { showModal("Cookie 说明", COOKIE_HELP); });
    $("mClose").addEventListener("click", function () { hideMask("mMask"); });
    $("qrClose").addEventListener("click", function () { hideMask("qrMask"); });
    $("quality").addEventListener("change", function () { rpc("set_config", ["quality", $("quality").value]); });
    var dismissBtn = $("ffmpegDismiss");
    if (dismissBtn) dismissBtn.addEventListener("click", function () {
      state.ffmpegIgnored = true;
      refreshFfmpegBanner();
      rpc("set_config", ["ffmpeg_ignored", true]).catch(function () {});
    });

    refreshRunState();
    // 载入配置
    rpc("get_config").then(function (r) {
      var cfg = (r && r.data) || {};
      if (cfg.platform) setPlatform(cfg.platform);
      else setPlatform("bilibili");
      if (cfg.mode) setMode(cfg.mode);
      if (cfg.quality) $("quality").value = cfg.quality;
      if (cfg.bilibili_cookie) $("cookie").value = cfg.bilibili_cookie;
      if (cfg.last_folder) $("folder").value = cfg.last_folder;
      if (cfg.ffmpeg_ignored) state.ffmpegIgnored = true;
    }).catch(function () { setPlatform("bilibili"); });
    rpc("get_status").then(function (r) {
      var st = (r && r.data) || {};
      state.ffmpeg = st.ffmpeg;
      refreshFfmpegWarn();
      refreshFfmpegBanner();
    }).catch(function () { refreshFfmpegBanner(); });

    setStatus("等待链接", "wait");
    applyRunningUI();
  }

  var COOKIE_HELP =
    "【Cookie 是什么？】登录 B 站后的身份凭证，带上它可解锁更高清晰度（1080P+/4K）与会员视频。\n" +
    "最关键是 SESSDATA。\n\n【推荐】直接点「扫码登录」，用哔哩哔哩 App 扫码确认即可自动填入。\n" +
    "【手动】电脑浏览器登录 B 站 → F12 → Application → Cookies → 复制 SESSDATA 值，粘贴为：SESSDATA=xxxx\n\n" +
    "注意：Cookie 等同登录态，请勿外传；不填也能下载，但清晰度受限为游客最高。";

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
