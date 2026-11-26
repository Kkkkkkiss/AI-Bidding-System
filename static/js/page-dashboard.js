/* 工作台：当天命中的招标信息 + 采集状态 */
(function () {
  "use strict";
  var h = App.h;

  App.routes.dashboard = function (main) {
    var box = h("div", null, App.empty("加载中…"));
    main.appendChild(box);

    App.get("/api/stats/dashboard").then(function (d) {
      App.clear(box);

      // 上手引导：三步配完就能自动跑
      var setup = [
        { done: d.setup.companies > 0, t: "填写公司信息", s: "公司名称、法人、地址", hash: "#/companies" },
        { done: d.setup.subscriptions > 0, t: "设置抓取关键词", s: "决定每天推什么标", hash: "#/companies" },
        { done: d.setup.notify, t: "接入群机器人", s: "企业微信/钉钉/飞书推送", hash: "#/settings" }
      ];
      var todo = setup.filter(function (x) { return !x.done; });
      if (todo.length) {
        box.appendChild(App.panel("开始使用（还剩 " + todo.length + " 步）",
          h("div", { class: "steps" }, setup.map(function (s) {
            return h("a", { class: "step" + (s.done ? " done" : ""), href: s.hash, style: { textDecoration: "none", color: "inherit" } }, h("b", null, (s.done ? "✓ " : "") + s.t), h("span", null, s.s));
          }))));
        box.lastChild.style.marginBottom = "16px";
      }

      var okSources = (d.sources || []).filter(function (s) { return s.enabled && s.last_status === "ok"; }).length;
      var allSources = (d.sources || []).filter(function (s) { return s.enabled; }).length;
      box.appendChild(h("div", { class: "kpis" },
        kpi(d.matches_new, "待处理的新招标", "未截止、还没处理", "#/notices"),
        kpi(d.notices_today, "今日新采集公告", "近 7 天 " + d.notices_7d + " 条", "#/notices?all=1"),
        kpi(okSources + " / " + allSources, "采集来源正常", "每小时自动抓一轮", "#/sources"),
        kpi(d.setup.subscriptions > 0 ? "已开" : "未设", "关键词匹配", "符合条件才会推给你", "#/companies")));

      var left = h("div");
      left.appendChild(App.panel("最新命中", d.top_matches.length ? h("div", { class: "nlist" }, d.top_matches.map(function (m) {
        return h("div", { class: "nitem", on: { click: function () { location.hash = "#/notices/" + m.id; } } },
          App.seal(m.deadline),
          h("div", null,
            h("div", { class: "title" }, m.title),
            h("div", { class: "meta" }, m.region ? h("span", null, m.region) : null, m.budget ? h("span", null, "预算 " + App.fmtAmount(m.budget)) : null)),
          h("div", { class: "side-r" }, h("div", { class: "score" }, Math.round(m.score), h("small", null, " 分"))));
      })) : App.empty("还没有命中", "设好关键词后，系统每天自动抓取并挑出符合你们公司的招标信息",
        h("a", { class: "btn", href: "#/companies" }, "去设置关键词")), h("a", { href: "#/notices" }, "全部招标信息")));

      var right = h("div");
      right.appendChild(App.panel("采集状态", App.table([
        { label: "来源", key: "name" },
        { label: "状态", render: function (s) {
          if (!s.enabled) return h("span", { class: "tag" }, "未启用");
          if (s.paused_until && s.paused_until > nowStr()) return h("span", { class: "tag red", title: s.last_message }, "暂停至 " + s.paused_until.slice(5, 16));
          if (s.last_status === "ok") return h("span", { class: "tag ok" }, "正常");
          if (!s.last_status) return h("span", { class: "tag" }, "等待首次运行");
          return h("span", { class: "tag amber", title: s.last_message }, s.last_status);
        } },
        { label: "上次运行", render: function (s) { return s.last_run_at ? s.last_run_at.slice(5, 16) : "—"; } }
      ], d.sources), App.can("manager", "sales") ? h("button", { class: "btn sm", on: { click: function () {
        if (!App.runCrawl) { location.hash = "#/sources"; return; }
        App.runCrawl();
      } } }, "立即采集一轮") : null,
      h("a", { href: "#/sources", style: { marginLeft: "8px" } }, "采集设置")));
      box.appendChild(h("div", { class: "grid2" }, left, right));
    });
  };

  function nowStr() {
    var d = new Date();
    function p(n) { return n < 10 ? "0" + n : "" + n; }
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  App.nowStr = nowStr;

  function kpi(v, k, s, hash) {
    return h("a", { class: "kpi", href: hash, style: { textDecoration: "none", color: "inherit" } }, h("div", { class: "v" }, String(v)), h("div", { class: "k" }, k), h("div", { class: "s" }, s));
  }
})();
