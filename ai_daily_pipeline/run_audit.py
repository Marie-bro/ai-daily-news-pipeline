"""Sidecar screening audit; never changes a pipeline decision or invokes a provider."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
import json
import os
import uuid
import hashlib
from types import SimpleNamespace
from html import escape
from urllib.parse import urlparse

from .text import article_id
from .exit_status import validate_exit_audit


def write_viewer(json_path: Path, data: dict) -> Path:
    """Local-only expandable read view; never part of the public H5 deployment."""
    records = data.get("articles", data.get("articles_107", []))
    extra = [x for x in data.get("accepted_13", []) if x["article_id"] not in {r["article_id"] for r in records}]
    records = [*records, *extra]
    sections = []
    for entry in records:
        ident = escape(str(entry.get("article_id", entry.get("trace_id", ""))))
        title = escape(str(entry.get("title", entry.get("title_en", ""))))
        source = escape(str(entry.get("source") or ""))
        status = escape(str(entry.get("screening_status", entry.get("final_status", entry.get("reason", "")))))
        url = str(entry.get("original_url") or "")
        safe_url = urlparse(url).scheme == "https"
        link = f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">Original URL</a>' if safe_url else escape(url)
        rows = "".join('<tr><td>'+escape(str(e.get("stage", "")))+'</td><td>'+escape(str(e.get("status", "")))+'</td><td>'+escape(str(e.get("reason", "")))+'</td><td><code>'+escape(json.dumps(e.get("detail") or {}, ensure_ascii=False))+'</code></td></tr>' for e in entry.get("events", []))
        sections.append(f'<details id="{ident}"><summary>{source} ? {title} <small>{status}</small></summary><p><code>{ident}</code> ? {link}</p><table><thead><tr><th>Stage</th><th>Status</th><th>Reason</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table></details>')
    summary = {k: v for k, v in data.items() if k not in {"articles", "articles_107", "accepted_13", "sources", "usage_rows"}}
    html = ('<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>MarieSpace Run Audit</title><style>body{font:15px/1.55 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#172332}'
            'details{border:1px solid #cbd5df;border-radius:8px;margin:.6rem 0;padding:.8rem}summary{cursor:pointer;font-weight:600}'
            'small{color:#516577}table{width:100%;border-collapse:collapse;overflow-wrap:anywhere}th,td{text-align:left;border-bottom:1px solid #ddd;padding:.4rem}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f8;padding:1rem}code{overflow-wrap:anywhere}</style>'
            '<h1>MarieSpace Run Audit</h1><p>Local audit only. Offline reconstruction is explicitly marked; no report was published by this viewer.</p>'
            '<h2>Run summary</h2><pre>'+escape(json.dumps(summary, ensure_ascii=False, indent=2))+'</pre><h2>Articles</h2>'
            + "".join(sections) + '</html>')
    target = json_path.with_suffix(".html")
    target.write_text(html, encoding="utf-8")
    return target


class RunAudit:
    def __init__(self, root: Path, started_at: datetime):
        self.root = Path(root)
        self.started_at = started_at
        self.run_id = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        self.sources = []
        self.traces = {}
        self.metrics = {}
        self.flows = {"collection": {}, "inventory_candidate": {}}
        self.final_status = "running"
        self._lock = Lock()

    def item(self, value):
        url = getattr(value, "url", getattr(value, "original_url", None))
        key = getattr(value, "id", None) or (article_id(url) if url else None)
        if key is None:
            return None
        with self._lock:
            trace = self.traces.setdefault(key, {"trace_id": key, "article_id": key, "source": None,
                "source_id": None, "original_url": url, "title": None, "published_at": None,
                "events": [], "current_stage": None, "final_status": "not_yet_selected", "final_reason": None})
            for field, attr in (("source", "source"), ("source_id", "source_id"),
                                ("title", "title"), ("published_at", "published_at")):
                val = getattr(value, attr, None)
                if val is not None:
                    trace[field] = val.isoformat() if isinstance(val, datetime) else val
            return key

    def event(self, value, stage: str, status: str, reason: str, detail=None):
        key = self.item(value)
        if key is None:
            return
        with self._lock:
            trace = self.traces[key]
            trace["current_stage"] = stage
            trace["events"].append({"stage": stage, "status": status, "reason": reason,
                                    "detail": detail or {}})
            if status == "dropped":
                trace["final_status"] = "dropped"
                trace["final_reason"] = reason
            elif status == "kept":
                trace["final_status"] = "publishable_candidate" if stage == "publishable" else "in_progress"
                trace["final_reason"] = None

    def parser_event(self, source, adapter, index, title, url, published_at, status, reason):
        # No URL is possible for malformed entries; an index-scoped ID remains stable for this response.
        ident = article_id(url) if url else hashlib.sha256(
            f"{source.source_id}:{adapter}:{index}:{title}".encode("utf-8")).hexdigest()
        entry = SimpleNamespace(id=ident, source_id=source.source_id, source=source.name,
                                title=title, original_url=url or None, published_at=published_at)
        self.event(entry, "adapter_" + adapter, status, reason, {"index": index})
        with self._lock:
            self.metrics["raw_index_entries_seen"] = self.metrics.get("raw_index_entries_seen", 0) + int(adapter in {"rss", "atom", "json", "html"})

    def source(self, source_id, status, count=0, detail=None):
        with self._lock:
            self.sources.append({"source_id": source_id, "status": status, "items": count,
                                 "detail": detail or {}})

    @staticmethod
    def _set(values):
        return {"count": len(values), "article_ids": sorted(values)}

    def collection_sets(self, source_items, articles, inserted_ids):
        """Record actual objects at collection boundaries; index entries are not a URL set."""
        self.flows["collection"] = {
            "raw_index": {"count": self.metrics.get("raw_index_entries_seen"),
                          "basis": "adapter parser decisions; not a unique article set"},
            "source_item": {"count": len(source_items),
                            "distinct_article_ids": sorted({article_id(item.url) for item in source_items}),
                            "basis": "SourceItem objects returned by adapters; URLs can repeat"},
            "current_run_article": {**self._set({item.id for item in articles}),
                                    "basis": "articles retained after extraction, freshness and collection cap"},
            "newly_inserted_article": {**self._set(set(inserted_ids)),
                                       "basis": "ArticleStore.add returned inserted=True"},
        }

    def inventory_origin_events(self, inventory):
        """Mark inventory origin before any later filter can drop the article."""
        run_ids = set(self.flows["collection"].get("current_run_article", {}).get("article_ids", []))
        for item in inventory:
            origin = "current_run_inventory" if item.id in run_ids else "historical_inventory"
            self.event(item, "inventory", "kept", origin, {"origin": origin})

    def inventory_sets(self, inventory, eligible, selected):
        """Classify inventory by observed current-run IDs, never by guessed source/date."""
        run_ids = set(self.flows["collection"].get("current_run_article", {}).get("article_ids", []))
        inventory_ids = {item.id for item in inventory}
        self.flows["inventory_candidate"] = {
            "inventory_total": {**self._set(inventory_ids),
                                "basis": "ArticleStore.supply_inventory result for the configured time range"},
            "current_run_inventory": {**self._set(inventory_ids & run_ids),
                                      "basis": "SQLite inventory ID intersected with current-run Article IDs"},
            "historical_inventory": {**self._set(inventory_ids - run_ids),
                                     "basis": "SQLite inventory IDs absent from current-run Article IDs"},
            "eligible_inventory": {**self._set({item.id for item in eligible}),
                                   "basis": "existing _eligible_content predicate"},
            "candidate_pool": {**self._set({key for key, trace in self.traces.items() if any(
                event["stage"] == "initial_candidate_pool" and event["status"] == "kept"
                for event in trace["events"])}),
                               "basis": "initial select() candidate_pool kept decisions"},
            "selected_candidate": {**self._set({item.id for item in selected}),
                                   "basis": "initial select() return value"},
            "enrichment_attempted": {"count": 0, "article_ids": [],
                                     "basis": "batch members, including token-guarded batches"},
            "model_submitted": {"count": 0, "article_ids": [], "batches": [],
                                "basis": "articles in calls actually submitted to DeepSeekClient.complete_json"},
        }

    def enrichment_sets(self, attempted):
        flow = self.flows["inventory_candidate"]
        flow["enrichment_attempted"].update(self._set({item.id for item in attempted}))

    def model_submitted(self, articles, batch_index):
        flow = self.flows["inventory_candidate"].get("model_submitted")
        if flow is None:
            return
        ids = [item.id for item in articles]
        flow["batches"].append({"batch_index": batch_index, "article_ids": ids})
        flow.update(self._set(set(flow["article_ids"]) | set(ids)))
        for item in articles:
            self.event(item, "model_request", "kept", "submitted", {"batch_index": batch_index})

    def save(self, status=None, **metrics):
        if "exit_code" in metrics:
            validate_exit_audit(status, metrics["exit_code"])
        if status is not None:
            self.final_status = status
        self.metrics.update(metrics)
        with self._lock:
            traces = list(self.traces.values())
            sources = list(self.sources)
        for trace in traces:
            if trace["final_status"] == "publishable_candidate":
                trace["final_status"] = "published" if self.metrics.get("report_id") else "not_published"
                trace["final_reason"] = None if self.metrics.get("report_id") else self.metrics.get("skipped_reason") or "run_ended_before_publication"
            elif trace["final_status"] in {"not_yet_selected", "in_progress"}:
                trace["final_status"] = "not_selected_or_not_reached"
                trace["final_reason"] = "run_ended_before_later_stages"
        reasons = Counter((e["stage"], e["reason"]) for t in traces for e in t["events"] if e["status"] == "dropped")
        data = {"run_id": self.run_id, "date": self.started_at.date().isoformat(),
                "start_time": self.started_at.isoformat(), "end_time": datetime.now(UTC).isoformat(),
                "sources_attempted": len(sources), "sources_ok": sum(s["status"] == "ok" for s in sources),
                "sources_empty": sum(s["status"] == "empty" for s in sources),
                "sources_failed": sum(s["status"] == "error" for s in sources),
                "sources": sources, "metrics": self.metrics, "flows": self.flows, "drop_reasons": [
                    {"stage": stage, "reason": reason, "count": count} for (stage, reason), count in sorted(reasons.items())],
                "final_status": self.final_status,
                "exit_code": self.metrics.get("exit_code"), "exit_reason": self.metrics.get("exit_reason"),
                "articles": sorted(traces, key=lambda t: t["trace_id"])}
        dest = self.root / "data" / "run-audits"
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / (self.run_id + ".json")
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
        write_viewer(path, data)
        return path
