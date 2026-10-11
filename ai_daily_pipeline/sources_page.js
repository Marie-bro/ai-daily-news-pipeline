const $ = id => document.getElementById(id);
const STAGES = ["collected", "normalized", "candidate", "enrichment", "validation", "publishable"];
const words = {collected:"采集", normalized:"标准化", candidate:"候选", enrichment:"整理", validation:"校验", publishable:"可发布"};
function node(tag, className, content) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (content !== undefined && content !== null) value.textContent = String(content);
  return value;
}
function time(value) {
  if (!value || value === "unavailable") return "unavailable";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? "unavailable" : parsed.toLocaleString("zh-CN", {timeZone:"Asia/Shanghai", hour12:false});
}
function traceLink(article) {
  const address = new URL(location.href);
  address.searchParams.set("trace_id", article.trace_id);
  const link = node("a", "", "Full trace / 完整轨迹");
  link.href = address.pathname + address.search;
  return link;
}
function articleCard(article, fullTrace = false) {
  const card = node("article", "article-card");
  const heading = node("div", "article-title");
  if (typeof article.original_url === "string" && article.original_url.startsWith("https://")) {
    const link = node("a", "", article.title);
    link.href = article.original_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    heading.append(link);
  } else heading.textContent = article.title;
  const meta = node("div", "article-meta");
  [article.source, time(article.published_at), `article_id: ${article.article_id}`,
   `trace_id: ${article.trace_id}`].forEach(value => meta.append(node("span", "", value)));
  if (article.source_mapping_basis !== "recorded")
    meta.append(node("span", "", `source mapping: ${article.source_mapping_basis}`));
  if (article.supply && Object.keys(article.supply).length) {
    const supply = node("details", "article-trace");
    supply.append(node("summary", "", `Supply Layer: ${article.supply.supply_layer || "unavailable"}`),
      node("pre", "", JSON.stringify(article.supply, null, 2)));
    meta.append(supply);
  }
  const trace = node("div", "trace");
  for (const stage of STAGES) {
    const status = article.stages[stage] || "unavailable";
    trace.append(node("span", `step ${status}`, `${words[stage]} · ${status}`));
  }
  const urlLine = node("div", "article-url");
  if (typeof article.original_url === "string" && article.original_url.startsWith("https://")) {
    const urlLink = node("a", "", article.original_url);
    urlLink.href = article.original_url;
    urlLink.target = "_blank";
    urlLink.rel = "noopener noreferrer";
    urlLine.append(urlLink);
  } else urlLine.textContent = "Original URL: unavailable";
  card.append(heading, meta, urlLine, trace);
  if (article.trace_id !== "unavailable") {
    if (!fullTrace) card.append(traceLink(article));
    else {
      const details = node("details", "article-trace");
      details.id = `trace-${article.trace_id}`;
      details.append(node("summary", "", "Full trace / 完整轨迹"));
      details.addEventListener("toggle", () => {
        if (!details.open || details.dataset.loaded) return;
        details.dataset.loaded = "true";
        if (article.screening_outcome)
          details.append(node("pre", "audit-json", JSON.stringify(article.screening_outcome, null, 2)));
        if (!article.events.length) details.append(node("p", "empty", "Trace events: unavailable"));
        for (const event of article.events) {
          const line = node("div", "trace-event");
          line.append(node("strong", "", `${event.stage}: ${event.status} · ${event.reason}`));
          line.append(node("pre", "", JSON.stringify(event.detail || {}, null, 2)));
          details.append(line);
        }
      });
      card.append(details);
    }
  }
  const outcome = article.screening_outcome;
  if (outcome) card.append(node("div", "article-meta", `${outcome.origin} · ${outcome.cache} · importance: ${outcome.importance_score} · model: ${outcome.model_submitted}`));
  const finalReason = outcome ? outcome.final_reason : article.reason;
  if (finalReason && finalReason !== "unavailable") card.append(node("div", "reason", `Final reason / 最终原因：${finalReason}`));
  return card;
}
function renderArticles(target, articles, fullTrace = false) {
  target.replaceChildren();
  if (!articles.length) { target.append(node("div", "empty", "此日期没有可查看的文章；不代表该来源未抓取。")); return; }
  let shown = 0;
  const more = node("button", "load-more");
  const append = () => {
    const fragment = document.createDocumentFragment();
    for (const article of articles.slice(shown, shown + 100)) fragment.append(articleCard(article, fullTrace));
    shown = Math.min(shown + 100, articles.length);
    more.textContent = `Show more / 加载更多 (${shown}/${articles.length})`;
    more.hidden = shown >= articles.length;
    target.insertBefore(fragment, more);
  };
  more.addEventListener("click", append);
  target.append(more);
  append();
}
function metric(number, label) {
  const box = node("div", "metric");
  box.append(node("strong", "", number), node("span", "", label));
  return box;
}
function auditTable(rows, fields) {
  const wrapper = node("div", "audit-scroll");
  const table = node("table");
  const head = node("tr");
  fields.forEach(field => head.append(node("th", "", field)));
  table.append(head);
  for (const row of rows) {
    const line = node("tr");
    fields.forEach(field => {
      const value = row[field] === undefined ? "not_observable" : row[field];
      line.append(node("td", "", typeof value === "object" ? JSON.stringify(value) : value));
    });
    table.append(line);
  }
  wrapper.append(table);
  return wrapper;
}
function renderInspection(view) {
  const target = $("inspection");
  target.replaceChildren(node("h2", "", "完整筛选过程 / Run Inspection"));
  if (!view) { target.append(node("p", "empty", "not_observable：没有本次 Run Audit")); return; }
  target.append(node("p", "source-explain", view.note));
  target.append(node("h3", "", "Today / Reserve"), node("pre", "audit-json", JSON.stringify(view.supply_summary, null, 2)));
  target.append(node("h3", "", "采集链路"), auditTable(view.collection, ["stage", "input", "kept", "dropped", "basis"]));
  target.append(node("h3", "", "库存 / 筛选链路"), node("p", "", `Current Run Inventory ${view.inventory_origins.current_run} + Historical Inventory ${view.inventory_origins.historical}`),
    auditTable(view.inventory, ["stage", "input", "kept", "dropped", "basis"]));
  target.append(node("h3", "", "模型 / 发布链路"), node("p", "", `Published (正式 readiness 已验证): ${view.published_count}`),
    auditTable(view.model_pipeline, ["stage", "input", "kept", "dropped", "not_reached", "basis"]),
    auditTable(view.batches, ["batch_id", "batch_size", "payload_bytes", "input_tokens", "output_tokens", "total_tokens", "finish_reason", "validation_passed", "repair_attempts", "repair_successes", "failure_type", "publishable_at_failure", "degraded_daily_entered"]));
  target.append(node("h3", "", "淘汰原因统计"), node("p", "source-explain", "实际决策次数；同一篇可能在多个阶段被观察，不能累加成独立文章数。"),
    auditTable(view.stages, ["stage", "input", "kept", "dropped", "other", "reasons"]));
  for (const batch of view.batches) {
    const details = node("details", "source-card");
    details.append(node("summary", "", `${batch.batch_id} · 模型请求与文章 IDs`),
      node("pre", "audit-json", JSON.stringify(batch, null, 2)));
    target.append(details);
  }
  for (const [label, key] of [["Selected Candidate（含补位）", "selected_candidates"], ["候选池最终去向", "candidate_outcomes"], ["最终日报来源解释", "published"]]) {
    const details = node("details", "source-card");
    details.append(node("summary", "", `${label} · ${view[key].length}`));
    details.addEventListener("toggle", () => {
      if (!details.open || details.dataset.loaded) return;
      details.dataset.loaded = "true";
      for (const row of view[key]) {
        const article = node("details", "article-card");
        article.append(node("summary", "", `${row.title} · ${row.final_reason}`));
        article.append(traceLink(row));
        article.append(node("pre", "audit-json", JSON.stringify(row, null, 2)));
        details.append(article);
      }
    });
    target.append(details);
  }
}
function render(data) {
  renderInspection(data.inspection);
  const configuredSources = data.sources.filter(item => item.source_id !== "unassigned");
  const parsedTotal = configuredSources.reduce((total, item) => total + (Number.isInteger(item.article_count) ? item.article_count : 0), 0);
  const knownCounts = configuredSources.every(item => Number.isInteger(item.article_count));
  const metrics = $("metrics");
  metrics.replaceChildren(metric(data.source_count, "当前启用来源"), metric(knownCounts ? parsedTotal : "unavailable", "当天已解析条目"),
    metric(data.visible_article_count, "可查看逐条记录"), metric(data.run_id, "运行 ID"));
  $("notice").textContent = `${data.notice} 来源状态依据：${data.source_status_provenance}；文章依据：${data.article_provenance}。`;
  const runSelect = $("run");
  runSelect.replaceChildren();
  data.runs.forEach(run => {
    const option = node("option", "", `${run.run_id} · ${run.mode}`);
    option.value = run.run_id;
    runSelect.append(option);
  });
  runSelect.value = data.run_id === "unavailable" ? "" : data.run_id;
  $("run-label").hidden = data.runs.length <= 1;
  const container = $("sources");
  container.replaceChildren();
  for (const source of data.sources) {
    const details = node("details", "source-card");
    const summary = node("summary");
    summary.append(node("span", "source-name", source.name), node("span", "source-meta",
      `${source.status} · 已解析 ${source.article_count} · 可查看 ${source.visible_article_count}`));
    const inner = node("div", "inner");
    inner.append(node("p", "source-explain", `${source.source_id} · ${source.region} · Tier ${source.tier} · ${source.source_role} · ${source.language}`));
    const list = node("div", "article-list");
    details.addEventListener("toggle", () => {
      if (details.open && !details.dataset.loaded) {
        details.dataset.loaded = "true";
        renderArticles(list, source.articles);
      }
    });
    inner.append(list);
    details.append(summary, inner);
    container.append(details);
  }
  renderArticles($("all-articles"), data.articles, true);
  const requestedTrace = new URL(location.href).searchParams.get("trace_id");
  const focused = $("focused-section");
  focused.hidden = true;
  if (requestedTrace) {
    const match = data.articles.find(item => item.trace_id === requestedTrace);
    if (match) {
      focused.hidden = false;
      renderArticles($("focused-trace"), [match], true);
      const details = focused.querySelector(".article-trace");
      if (details) { details.open = true; focused.scrollIntoView({block:"start"}); }
    }
  }
}
async function load(date, runId) {
  $("loading").textContent = "读取已保存数据…";
  const params = new URLSearchParams({date});
  if (runId) params.set("run_id", runId);
  try {
    const response = await fetch(`/api/sources?${params}`, {cache:"no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法读取审计数据");
    render(data);
    const address = new URL(location.href);
    address.searchParams.set("date", date);
    if (runId) address.searchParams.set("run_id", runId); else address.searchParams.delete("run_id");
    if (address.searchParams.get("trace_id") && !data.articles.some(item => item.trace_id === address.searchParams.get("trace_id")))
      address.searchParams.delete("trace_id");
    history.replaceState(null, "", address);
    $("loading").textContent = "只读 · 本地";
  } catch (error) { $("loading").textContent = error.message; }
}
async function start() {
  const response = await fetch("/api/sources/dates", {cache:"no-store"});
  const dates = await response.json();
  const url = new URL(location.href);
  const day = url.searchParams.get("date") || dates.dates[0] || new Date().toISOString().slice(0,10);
  $("date").value = day;
  $("date").addEventListener("change", () => load($("date").value));
  $("run").addEventListener("change", () => load($("date").value, $("run").value));
  await load(day, url.searchParams.get("run_id"));
}
start().catch(error => { $("loading").textContent = error.message; });
