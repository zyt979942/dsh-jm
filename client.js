/**
 * dsh-jm 的设置页（设置 → JM 个性化）。
 *
 * 浏览器里的 React 组件，能力全在 node 那边：
 *   · GET  /dsh-jm/prefs    读配置
 *   · POST /dsh-jm/prefs    写配置
 *   · POST /dsh-jm/analyze  扫目录、写 个性化分析.md
 *
 * ⚠️ 这个文件是纯 JS，没有构建步骤，所以用 h(...) 手写，不是 JSX。
 */
window.__ModuleLoader__.load({ id: "dsh-jm", factory: (require) => {
  var module = { exports: {} };
  var exports = module.exports;
  Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

  var react = require("react");
  var h = react.createElement;
  var useState = react.useState;
  var useEffect = react.useEffect;
  var useCallback = react.useCallback;

  var name = "dsh-jm";
  var inject = ["slots", "locale"];
  var NS = "settings.dsh-jm";

  async function api(path, init) {
    var res = await fetch(path, Object.assign({
      cache: "no-store",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
    }, init || {}));
    var text = await res.text();
    var data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
    return data || {};
  }

  var CSS = [
    ".jmprefs{display:flex;flex-direction:column;gap:16px}",
    ".jmprefs h2{margin:0;font-size:20px;line-height:28px;font-weight:600;color:var(--dsw-alias-label-primary)}",
    ".jmprefs .lead{margin:0;font-size:13px;line-height:20px;color:var(--dsw-alias-label-tertiary)}",
    ".jmprefs .card{border:.5px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-settings-card-fill,transparent);border-radius:16px;padding:16px;display:flex;flex-direction:column;gap:10px}",
    ".jmprefs label{font-size:13px;font-weight:600;color:var(--dsw-alias-label-primary)}",
    ".jmprefs .hint{font-size:12px;line-height:18px;color:var(--dsw-alias-label-tertiary)}",
    ".jmprefs input,.jmprefs textarea{width:100%;box-sizing:border-box;font-size:13px;padding:10px 12px;border-radius:10px;border:.5px solid var(--dsw-alias-border-l2);background:rgba(127,127,127,.06);color:var(--dsw-alias-label-primary);outline:none}",
    ".jmprefs input:focus,.jmprefs textarea:focus{border-color:#4176e6}",
    ".jmprefs textarea{min-height:200px;resize:vertical;font-family:ui-monospace,Menlo,Consolas,monospace;line-height:20px}",
    ".jmprefs .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}",
    ".jmprefs button{font-size:13px;padding:8px 14px;border-radius:10px;cursor:pointer;border:.5px solid var(--dsw-alias-border-l2);background:rgba(127,127,127,.10);color:var(--dsw-alias-label-primary)}",
    ".jmprefs button.primary{background:#4176e6;color:#fff;border-color:transparent}",
    ".jmprefs button:disabled{opacity:.5;cursor:default}",
    ".jmprefs .msg{font-size:12px;line-height:18px}",
    ".jmprefs .ok{color:#22c55e}",
    ".jmprefs .bad{color:#ef4444}",
  ].join("\n");

  function SettingsPage() {
    var s = useState({ libraryPath: "", favoritePath: "", seen: "" });
    var cfg = s[0];
    var setCfg = s[1];
    var m = useState("");
    var msg = m[0];
    var setMsg = m[1];
    var k = useState("");
    var kind = k[0];
    var setKind = k[1];
    var b = useState(false);
    var busy = b[0];
    var setBusy = b[1];
    var i = useState("");
    var info = i[0];
    var setInfo = i[1];

    var say = function (text, kind2) { setMsg(text); setKind(kind2 || ""); };

    useEffect(function () {
      api("/dsh-jm/prefs").then(function (d) {
        if (d && d.prefs) setCfg({ libraryPath: d.prefs.libraryPath || "", favoritePath: d.prefs.favoritePath || "", seen: d.prefs.seen || "" });
      }).catch(function (e) { say("读配置失败：" + e.message, "bad"); });
    }, []);

    var save = useCallback(function () {
      setBusy(true);
      api("/dsh-jm/prefs", { method: "POST", body: JSON.stringify({ patch: cfg }) })
        .then(function (d) { say("已保存。已记录 " + (d.seenCount || 0) + " 个 jm 号。", "ok"); })
        .catch(function (e) { say("保存失败：" + e.message, "bad"); })
        .then(function () { setBusy(false); });
    }, [cfg]);

    var recordFav = useCallback(function () {
      setBusy(true);
      setInfo("");
      api("/dsh-jm/record-favorites", { method: "POST", body: JSON.stringify({ path: cfg.favoritePath }) })
        .then(function (d) {
          setInfo("已入库 " + d.moved + " 张（有效分析 " + d.analyzed + " 张），文件已挪到「已经分析的」。");
          say("入库完成。", "ok");
        })
        .catch(function (e) { say("入库失败：" + e.message, "bad"); })
        .then(function () { setBusy(false); });
    }, [cfg.favoritePath]);

    var recommend = useCallback(function () {
      setBusy(true);
      setInfo("正在生成推荐（约 30 秒，要联网查候选和封面）…");
      api("/dsh-jm/recommend", {
        method: "POST",
        body: JSON.stringify({ top: 10, path: cfg.libraryPath, favoritePath: cfg.favoritePath }),
      })
        .then(function (d) {
          setInfo("候选 " + d.candidates + " 本，取前 " + d.count + " 本。报告：" + d.report);
          say("推荐已生成。", "ok");
        })
        .catch(function (e) { say("推荐失败：" + e.message, "bad"); })
        .then(function () { setBusy(false); });
    }, [cfg.libraryPath, cfg.favoritePath]);

    var analyze = useCallback(function () {
      setBusy(true);
      setInfo("");
      api("/dsh-jm/analyze", { method: "POST", body: JSON.stringify({ path: cfg.libraryPath, favoritePath: cfg.favoritePath }) })
        .then(function (d) {
          setInfo("扫到 " + d.books + " 本 / " + d.pages + " 页 / " + d.mb + " MB，报告已写入：" + d.report);
          say("分析完成。", "ok");
        })
        .catch(function (e) { say("分析失败：" + e.message, "bad"); })
        .then(function () { setBusy(false); });
    }, [cfg.libraryPath]);

    return h("div", { className: "jmprefs" },
      h("style", null, CSS),
      h("h2", null, "JM 个性化"),
      h("p", { className: "lead" }, "告诉插件你的收藏长什么样、哪些已经看过，推荐和下本时就不会重复。"),
      h("div", { className: "card" },
        h("label", null, "漫画库文件夹"),
        h("input", {
          type: "text",
          value: cfg.libraryPath,
          placeholder: "C:\\Users\\你的名字\\Desktop",
          onChange: function (e) { setCfg(Object.assign({}, cfg, { libraryPath: e.target.value })); },
        }),
        h("div", { className: "hint" }, "填一个放着漫画/色图的目录。会在该目录里生成「个性化分析.md」，不改动任何图片。"),
        h("div", { className: "row" },
          h("button", { type: "button", className: "primary", disabled: busy, onClick: analyze }, busy ? "处理中…" : "分析这个目录"),
          h("button", { type: "button", disabled: busy, onClick: save }, "保存路径"),
        ),
        info ? h("div", { className: "hint" }, info) : null,
      ),
      h("div", { className: "card" },
        h("label", null, "精选文件夹（可选，但强烈建议）"),
        h("input", {
          type: "text",
          value: cfg.favoritePath,
          placeholder: "C:\\Users\\你的名字\\Desktop\\我的精选",
          onChange: function (e) { setCfg(Object.assign({}, cfg, { favoritePath: e.target.value })); },
        }),
        h("div", { className: "hint" },
          "自己新建一个文件夹，把觉得好的、高潮的那几页复制进去。分析会拿这批图算你的真实画风偏好 —— ",
          "比全库平均准得多，因为全库只能说明你收藏了什么。"),
        h("div", { className: "row" },
          h("button", { type: "button", disabled: busy, onClick: recordFav }, busy ? "处理中…" : "入库这批图"),
          h("button", { type: "button", disabled: busy, onClick: save }, "保存精选路径"),
        ),
        h("div", { className: "hint" },
          "「入库」会把「还没分析的」里的图算进画风基准，然后挪到「已经分析的」——挪走是为了不让它们下次被重复计算。"),
      ),

      h("div", { className: "card" },
        h("label", null, "已下载 / 看过的漫画"),
        h("textarea", {
          value: cfg.seen,
          placeholder: "一行一个 jm 号，或者把带标题的整段粘进来，会自动抓数字",
          onChange: function (e) { setCfg(Object.assign({}, cfg, { seen: e.target.value })); },
        }),
        h("div", { className: "hint" }, "用来避免个性化推荐重复。全文抓取 5~8 位数字，格式随意。"),
        h("div", { className: "row" },
          h("button", { type: "button", className: "primary", disabled: busy, onClick: save }, "保存清单"),
        ),
      ),
      h("div", { className: "card" },
        h("label", null, "个性化推荐"),
        h("div", { className: "hint" },
          "根据收藏库的标签偏好 + 精选目录的画风基准，从站内挑没下过的本子，打分排序并写明依据。"),
        h("div", { className: "row" },
          h("button", { type: "button", className: "primary", disabled: busy, onClick: recommend },
            busy ? "处理中…" : "生成推荐"),
        ),
      ),

      msg ? h("div", { className: "msg " + (kind === "bad" ? "bad" : "ok") }, msg) : null,
    );
  }

  function apply(ctx) {
    // 字典：设置外壳会按 locale 取 section 的名字。
    // 只给 label 也能显示，但官方契约里 label/locale/inject 是一组，
    // 缺 locale 时有些版本会退化成显示 id（dsh-jm），所以老实注册。
    var dict = { "page.nav": "JM 个性化", "page.title": "JM 个性化" };
    var t = function (key) { return dict[key] || key; };
    try {
      ctx.effect(function () { return ctx.locale.register(NS, { zh: dict, en: dict }); }, "dsh-jm: dictionaries");
      t = ctx.locale.bind(NS);
    } catch (e) {
      try { console.warn("[dsh-jm] 字典注册失败:", e); } catch (e2) { /* ignore */ }
    }

    try {
      ctx.slots.inject("settings.section", function () {
        return ctx.slots.register({
          name: "settings.section",
          id: "dsh-jm",
          order: 470,
          label: function () { return t("page.nav"); },
          locale: NS,
          inject: function () { return { t: t }; },
        }, SettingsPage);
      });
    } catch (e) {
      try { console.warn("[dsh-jm] 设置页注册失败:", e); } catch (e2) { /* ignore */ }
    }
  }

  exports.name = name;
  exports.inject = inject;
  exports.apply = apply;
  return module.exports;
  }
});
