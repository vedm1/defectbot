"""
UAT Defect Triage Board — kanban view + bulk actions.

Endpoints (all under /triage):
  GET  /triage                — render the board with current bug state.
  POST /triage/bulk-state     — apply a state change to N selected bugs.
  POST /triage/mark-duplicates — mark N bugs as duplicates of a canonical, close them.

Design notes:
  * Server-side renders the initial HTML with all bug data baked in. The
    client-side JS handles filtering, selection, cluster lens, and the
    Mark-as-duplicate modal entirely client-side.
  * Posts use form-encoded data and the route does a 303 redirect back to
    /triage. Keeps the model simple — no client-side JSON wrangling.
  * Cache is busted automatically by the ADO helper functions after a
    successful state change.
"""
from __future__ import annotations

import html
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from ado_client import (
    bulk_update_state,
    list_uat_defects,
    mark_as_duplicates,
)

log = logging.getLogger("defect-agent.triage")

router = APIRouter(prefix="/triage")

# Columns we render. ADO Bug state transitions in North Star are
# New / In Progress / Done; we also surface Approved if any bugs are in it.
COLUMNS = ["New", "Approved", "In Progress", "Done"]
# States offered in the state-change dropdowns (excludes Removed for safety;
# Removed bugs are filtered from the board entirely by default).
STATE_OPTIONS = ["New", "Approved", "In Progress", "Done", "Removed"]


_SEVERITY_RANK = {"1 - Critical": 1, "2 - High": 2, "3 - Medium": 3, "4 - Low": 4}


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _relative_time(iso: str) -> str:
    """ '2026-05-15T05:35:55.47Z' -> 'just now', '12m ago', '3h ago', '4d ago'. """
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return ""
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 30 * 86400:
        return f"{int(delta // 86400)}d ago"
    months = int(delta // (30 * 86400))
    return f"{months}mo ago"


def _ado_url(bug_id: int) -> str:
    org = os.environ.get("ADO_ORG", "euromonitor")
    return f"https://dev.azure.com/{org}/North%20Star/_workitems/edit/{bug_id}"


def _render_card(bug: dict) -> str:
    sev = bug.get("severity") or ""
    sev_rank = _SEVERITY_RANK.get(sev, 5)
    sev_class = f"sev sev-{sev_rank}"
    sev_label = _esc(sev or "—")

    feature_html = ""
    if bug.get("featureId"):
        ft = _esc(bug.get("featureTitle") or "")[:60]
        feature_html = (
            f'<a class="pill feature" target="_blank" '
            f'href="{_ado_url(bug["featureId"])}">'
            f'<span class="pill-ico">⌗</span> {_esc(bug["featureId"])}: {ft}</a>'
        )
    else:
        feature_html = '<span class="pill feature-none">⌗ no Feature</span>'

    rel_count = len(bug.get("relatedBugs") or [])
    related_html = ""
    if rel_count:
        items = "".join(
            f'<a target="_blank" href="{_ado_url(r["id"])}">'
            f'#{r["id"]} — {_esc(r["title"])[:60]} <em>({_esc(r.get("state", ""))})</em></a>'
            for r in bug["relatedBugs"]
        )
        related_html = (
            f'<details class="related"><summary>🔗 Related ({rel_count})</summary>'
            f'<div class="related-list">{items}</div></details>'
        )

    dup_html = ""
    if bug.get("duplicateOf"):
        dup_html = (
            f'<a class="pill duplicate" target="_blank" '
            f'href="{_ado_url(bug["duplicateOf"])}">'
            f'duplicate of #{bug["duplicateOf"]}</a>'
        )

    cluster_html = ""
    if bug.get("clusterTag"):
        cluster_html = f'<span class="cluster">{_esc(bug["clusterTag"])}</span>'

    state_options_html = "".join(
        f'<option value="{_esc(s)}"' +
        (' selected' if s == bug.get("state") else '') +
        f'>{_esc(s)}</option>'
        for s in STATE_OPTIONS
    )

    return f"""
<div class="card" data-bug-id="{bug["id"]}" data-state="{_esc(bug.get("state",""))}"
     data-severity="{_esc(sev)}" data-feature="{bug.get("featureId") or ''}"
     data-cluster="{_esc(bug.get("clusterTag",""))}"
     data-title="{_esc(bug.get("title",""))}">
  <header>
    <input type="checkbox" class="card-select" data-bug-id="{bug["id"]}" aria-label="Select #{bug['id']}">
    <a class="bug-id" target="_blank" href="{_ado_url(bug["id"])}">#{bug["id"]}</a>
    <span class="{sev_class}">{sev_label}</span>
    {dup_html}
  </header>
  <h3>{_esc(bug.get("title",""))}</h3>
  <div class="meta">
    {feature_html}
    {related_html}
  </div>
  {cluster_html}
  <footer>
    <span class="reporter">{_esc(bug.get("createdBy",""))}</span>
    <span class="time">· {_esc(_relative_time(bug.get("createdDate","")))}</span>
    <form method="post" action="/triage/bulk-state" class="state-form">
      <input type="hidden" name="bug_ids" value="{bug["id"]}">
      <select name="new_state" onchange="this.form.submit()" aria-label="Change state">
        {state_options_html}
      </select>
    </form>
  </footer>
</div>"""


def _render_columns(defects: list[dict]) -> str:
    """Render kanban columns side by side. Hidden cards still render in DOM
    so client-side filters can show/hide without a round-trip."""
    by_state: dict[str, list[dict]] = {c: [] for c in COLUMNS}
    other_state: list[dict] = []
    for b in defects:
        s = b.get("state") or "New"
        if s == "Removed":
            # Removed bugs are still in the DOM but get a hidden class,
            # toggleable via the "Show Removed" filter.
            b["_removed"] = True
            other_state.append(b)
        elif s in by_state:
            by_state[s].append(b)
        else:
            # Unrecognised state — fall back to a generic "Other" column.
            other_state.append(b)

    parts = []
    for col in COLUMNS:
        cards = by_state[col]
        cards_html = "\n".join(_render_card(b) for b in cards) or \
            '<div class="empty">No defects in this state.</div>'
        parts.append(
            f'<div class="column" data-state="{_esc(col)}">'
            f'  <header><h2>{_esc(col)} <span class="count">{len(cards)}</span></h2></header>'
            f'  <div class="cards">{cards_html}</div>'
            f'</div>'
        )
    if other_state:
        cards_html = "\n".join(
            _render_card(b) for b in other_state
            if not b.get("_removed")
        )
        if cards_html:
            parts.append(
                f'<div class="column other" data-state="Other">'
                f'  <header><h2>Other <span class="count">{len(other_state)}</span></h2></header>'
                f'  <div class="cards">{cards_html}</div>'
                f'</div>'
            )
        # Removed cards live inside a hidden bucket; JS shows them via a toggle.
        removed_cards = "\n".join(
            _render_card(b) for b in other_state if b.get("_removed")
        )
        if removed_cards:
            parts.append(
                f'<div class="column removed-col hidden" data-state="Removed">'
                f'  <header><h2>Removed <span class="count">'
                f'{sum(1 for b in other_state if b.get("_removed"))}</span></h2></header>'
                f'  <div class="cards">{removed_cards}</div>'
                f'</div>'
            )
    return "\n".join(parts)


_BOARD_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
       background: #f3f3f6; color: #1e1e1e; margin: 0; padding: 0; }
.topbar { background: #fff; border-bottom: 1px solid #e1e1e6; padding: 14px 24px;
       display: flex; align-items: center; gap: 14px; position: sticky; top: 0; z-index: 5; }
.topbar h1 { font-size: 18px; margin: 0; font-weight: 600; }
.topbar .grow { flex: 1; }
.topbar a { color: #6264a7; text-decoration: none; font-size: 14px; }
.filters { background: #fff; border-bottom: 1px solid #e1e1e6; padding: 12px 24px;
       display: flex; flex-wrap: wrap; gap: 12px; align-items: center; font-size: 13px;
       position: sticky; top: 50px; z-index: 4; }
.filters input[type=search], .filters select {
       padding: 7px 10px; border: 1px solid #d4d4d8; border-radius: 6px;
       background: #fff; font-size: 13px; font-family: inherit; }
.filters input[type=search] { min-width: 240px; }
.filters .sev-chips { display: flex; gap: 6px; }
.filters .sev-chip { padding: 5px 10px; border: 1px solid #d4d4d8;
       border-radius: 14px; cursor: pointer; font-size: 12px; user-select: none;
       background: #fff; }
.filters .sev-chip.active { background: #6264a7; color: #fff; border-color: #6264a7; }
.filters label.tog { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
.bulkbar { background: #ecf0fb; padding: 10px 24px; display: none;
       align-items: center; gap: 10px; border-bottom: 1px solid #c8d0ed;
       position: sticky; top: 102px; z-index: 4; }
.bulkbar.visible { display: flex; }
.bulkbar .count { font-weight: 600; color: #3d3f8a; }
.bulkbar button, .bulkbar select { padding: 7px 12px; font-size: 13px;
       border: 1px solid #6264a7; border-radius: 6px; background: #fff;
       color: #3d3f8a; cursor: pointer; font-family: inherit; }
.bulkbar button.primary { background: #6264a7; color: #fff; }
.bulkbar button.danger { background: #fff; color: #7b1d1d; border-color: #d49a9a; }
.board { display: flex; gap: 14px; padding: 18px 24px; overflow-x: auto;
       align-items: flex-start; }
.column { background: #ececf0; border-radius: 10px; padding: 8px;
       width: 320px; min-width: 320px; flex-shrink: 0; }
.column.hidden { display: none; }
.column > header { padding: 6px 10px; }
.column h2 { margin: 0; font-size: 14px; font-weight: 600; color: #444;
       display: flex; align-items: center; gap: 6px; }
.column h2 .count { background: #d4d4d8; padding: 1px 8px; border-radius: 10px;
       font-size: 12px; font-weight: 600; }
.cards { display: flex; flex-direction: column; gap: 8px; margin-top: 6px;
       max-height: calc(100vh - 230px); overflow-y: auto; padding: 2px; }
.empty { font-size: 12px; color: #999; text-align: center; padding: 20px; }
.card { background: #fff; border-radius: 8px; padding: 10px 12px;
       box-shadow: 0 1px 2px rgba(0,0,0,.04), 0 2px 6px rgba(0,0,0,.04);
       border-left: 3px solid transparent; font-size: 13px; }
.card.hidden { display: none; }
.card.selected { border-color: #6264a7; box-shadow: 0 0 0 2px #c4caf0; }
.card header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.card h3 { margin: 4px 0; font-size: 14px; font-weight: 500; line-height: 1.35;
       display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
       overflow: hidden; }
.card .bug-id { font-weight: 600; color: #6264a7; text-decoration: none; }
.card-select { accent-color: #6264a7; }
.sev { padding: 1px 7px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.sev-1 { background: #fee2e2; color: #991b1b; }
.sev-2 { background: #fed7aa; color: #9a3412; }
.sev-3 { background: #fef3c7; color: #854d0e; }
.sev-4 { background: #e5e5e5; color: #595959; }
.sev-5 { background: #f5f5f5; color: #999; }
.card .meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.pill { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px;
       border-radius: 12px; font-size: 11px; text-decoration: none; }
.pill.feature { background: #e8f4fc; color: #075985; max-width: 100%;
       overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pill.feature-none { background: #f3f4f6; color: #999; font-style: italic; }
.pill.duplicate { background: #fef2f2; color: #991b1b; font-weight: 600; }
.related { font-size: 11px; }
.related summary { cursor: pointer; padding: 2px 8px; background: #f3e8ff;
       color: #6b21a8; border-radius: 12px; display: inline-block; }
.related summary::-webkit-details-marker { display: none; }
.related-list { margin-top: 6px; padding: 8px; background: #faf5ff;
       border-radius: 6px; display: flex; flex-direction: column; gap: 4px; }
.related-list a { color: #6b21a8; text-decoration: none; font-size: 11px; }
.related-list a em { color: #999; font-style: normal; }
.cluster { display: inline-block; margin-top: 6px; padding: 1px 7px;
       background: #f3f4f6; color: #555; border-radius: 8px; font-size: 10px;
       font-family: 'SF Mono', 'Courier New', monospace; }
.card footer { display: flex; align-items: center; gap: 6px; margin-top: 8px;
       font-size: 11px; color: #777; }
.card footer .state-form { margin-left: auto; }
.card footer select { padding: 2px 6px; font-size: 11px; border: 1px solid #d4d4d8;
       border-radius: 4px; background: #fff; font-family: inherit; }
.cluster-view .column { display: none; }
.cluster-view .cluster-group { background: #fff; border-radius: 10px;
       margin: 10px 24px; padding: 14px; }
.cluster-view .cluster-group h3 { margin: 0 0 10px; font-size: 14px;
       color: #6b21a8; font-family: 'SF Mono', monospace; }
.cluster-view .cluster-group .cards { display: grid;
       grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
       gap: 10px; max-height: none; }
.modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,.4); display: none;
       align-items: center; justify-content: center; z-index: 100; }
.modal-bg.visible { display: flex; }
.modal { background: #fff; border-radius: 10px; padding: 24px; max-width: 480px;
       width: 90%; box-shadow: 0 12px 40px rgba(0,0,0,.2); }
.modal h2 { margin: 0 0 12px; font-size: 16px; }
.modal p { font-size: 13px; color: #555; margin: 0 0 14px; line-height: 1.5; }
.modal label { display: block; margin-top: 12px; font-size: 13px; font-weight: 600; }
.modal select { width: 100%; padding: 8px; margin-top: 4px; font-size: 14px;
       border: 1px solid #d4d4d8; border-radius: 6px; }
.modal .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.modal button { padding: 9px 18px; border: 0; border-radius: 6px; font-size: 13px;
       font-weight: 600; cursor: pointer; font-family: inherit; }
.modal button.secondary { background: #f3f3f6; color: #333; }
.modal button.primary { background: #6264a7; color: #fff; }
.banner { background: #ecfdf3; border: 1px solid #b2efc2; color: #15532b;
       padding: 12px 24px; font-size: 13px; }
.banner.err { background: #fef2f2; border-color: #fecaca; color: #7b1d1d; }
"""

_BOARD_JS = r"""
(function() {
  const board = document.querySelector('.board');
  const cards = Array.from(document.querySelectorAll('.card'));
  const search = document.getElementById('search');
  const sevChips = document.querySelectorAll('.sev-chip');
  const featureFilter = document.getElementById('feature-filter');
  const showRemoved = document.getElementById('show-removed');
  const clusterLens = document.getElementById('cluster-lens');
  const bulkbar = document.querySelector('.bulkbar');
  const bulkCount = document.getElementById('bulk-count');
  const bulkState = document.getElementById('bulk-state');
  const bulkApply = document.getElementById('bulk-apply');
  const bulkDup = document.getElementById('bulk-dup');
  const bulkClear = document.getElementById('bulk-clear');
  const modal = document.getElementById('dup-modal');
  const modalCancel = document.getElementById('dup-cancel');
  const modalConfirm = document.getElementById('dup-confirm');
  const canonicalSelect = document.getElementById('canonical-select');

  let selected = new Set();
  let activeSevs = new Set();

  // ----- filters -----
  function applyFilters() {
    const q = (search.value || '').toLowerCase().trim();
    const feat = featureFilter.value;  // '' = all
    const showRem = showRemoved.checked;
    cards.forEach(card => {
      const title = (card.dataset.title || '').toLowerCase();
      const sev = card.dataset.severity || '';
      const feature = card.dataset.feature || '';
      const state = card.dataset.state || '';
      let visible = true;
      if (q && !title.includes(q) && !card.dataset.bugId.includes(q)) visible = false;
      if (activeSevs.size && !activeSevs.has(sev)) visible = false;
      if (feat && feature !== feat) visible = false;
      if (!showRem && state === 'Removed') visible = false;
      card.classList.toggle('hidden', !visible);
    });
    // Update column counts
    document.querySelectorAll('.column').forEach(col => {
      const v = col.querySelectorAll('.card:not(.hidden)').length;
      const counter = col.querySelector('header .count');
      if (counter) counter.textContent = v;
    });
    // Show/hide Removed column
    document.querySelector('.column.removed-col')?.classList.toggle('hidden', !showRem);
  }

  search.addEventListener('input', applyFilters);
  featureFilter.addEventListener('change', applyFilters);
  showRemoved.addEventListener('change', applyFilters);
  sevChips.forEach(chip => chip.addEventListener('click', () => {
    const v = chip.dataset.sev;
    if (activeSevs.has(v)) activeSevs.delete(v); else activeSevs.add(v);
    chip.classList.toggle('active', activeSevs.has(v));
    applyFilters();
  }));

  // ----- selection -----
  function refreshBulkbar() {
    bulkCount.textContent = selected.size;
    bulkbar.classList.toggle('visible', selected.size > 0);
    // populate canonical dropdown
    canonicalSelect.innerHTML = '';
    selected.forEach(id => {
      const card = document.querySelector('.card[data-bug-id="' + id + '"]');
      if (!card) return;
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = '#' + id + ' — ' + (card.dataset.title || '').slice(0, 80);
      canonicalSelect.appendChild(opt);
    });
  }
  document.querySelectorAll('.card-select').forEach(cb => {
    cb.addEventListener('change', e => {
      const id = e.target.dataset.bugId;
      const card = e.target.closest('.card');
      if (e.target.checked) selected.add(id); else selected.delete(id);
      card.classList.toggle('selected', e.target.checked);
      refreshBulkbar();
    });
  });
  bulkClear.addEventListener('click', () => {
    selected.clear();
    document.querySelectorAll('.card-select').forEach(cb => { cb.checked = false; });
    document.querySelectorAll('.card.selected').forEach(c => c.classList.remove('selected'));
    refreshBulkbar();
  });

  // ----- bulk state change -----
  bulkApply.addEventListener('click', () => {
    const newState = bulkState.value;
    if (!newState || selected.size === 0) return;
    if (!confirm('Move ' + selected.size + ' bug(s) to "' + newState + '"?')) return;
    const form = document.createElement('form');
    form.method = 'post';
    form.action = '/triage/bulk-state';
    selected.forEach(id => {
      const i = document.createElement('input');
      i.type = 'hidden'; i.name = 'bug_ids'; i.value = id;
      form.appendChild(i);
    });
    const ns = document.createElement('input');
    ns.type = 'hidden'; ns.name = 'new_state'; ns.value = newState;
    form.appendChild(ns);
    document.body.appendChild(form);
    form.submit();
  });

  // ----- mark-as-duplicate flow -----
  bulkDup.addEventListener('click', () => {
    if (selected.size < 2) {
      alert('Select at least 2 bugs: one canonical and one+ duplicates.');
      return;
    }
    refreshBulkbar();
    modal.classList.add('visible');
  });
  modalCancel.addEventListener('click', () => modal.classList.remove('visible'));
  modalConfirm.addEventListener('click', () => {
    const canonical = canonicalSelect.value;
    if (!canonical) return;
    const dupes = Array.from(selected).filter(id => id !== canonical);
    if (dupes.length === 0) {
      alert('No duplicates would be created — canonical is the only selection.');
      return;
    }
    if (!confirm('Mark ' + dupes.length + ' bug(s) as duplicates of #' + canonical + ' and close them?')) return;
    const form = document.createElement('form');
    form.method = 'post';
    form.action = '/triage/mark-duplicates';
    const c = document.createElement('input');
    c.type = 'hidden'; c.name = 'canonical_id'; c.value = canonical;
    form.appendChild(c);
    dupes.forEach(id => {
      const i = document.createElement('input');
      i.type = 'hidden'; i.name = 'duplicate_ids'; i.value = id;
      form.appendChild(i);
    });
    document.body.appendChild(form);
    form.submit();
  });

  // ----- cluster lens -----
  clusterLens.addEventListener('change', () => {
    if (clusterLens.checked) {
      // Group by clusterTag
      const groups = {};
      cards.forEach(card => {
        const c = card.dataset.cluster || '(no cluster)';
        if (!groups[c]) groups[c] = [];
        groups[c].push(card);
      });
      const sorted = Object.keys(groups).sort((a, b) => groups[b].length - groups[a].length);
      const container = document.getElementById('cluster-container');
      container.innerHTML = '';
      sorted.forEach(c => {
        const div = document.createElement('div');
        div.className = 'cluster-group';
        const head = document.createElement('h3');
        head.textContent = c + '  (' + groups[c].length + ')';
        div.appendChild(head);
        const wrap = document.createElement('div');
        wrap.className = 'cards';
        groups[c].forEach(card => wrap.appendChild(card.cloneNode(true)));
        div.appendChild(wrap);
        container.appendChild(div);
      });
      board.classList.add('cluster-view');
      container.style.display = 'block';
      // Re-bind selection on clones (they're new DOM nodes)
      bindSelection();
    } else {
      board.classList.remove('cluster-view');
      document.getElementById('cluster-container').innerHTML = '';
      document.getElementById('cluster-container').style.display = 'none';
    }
  });

  function bindSelection() {
    document.querySelectorAll('.card-select').forEach(cb => {
      cb.onchange = (e) => {
        const id = e.target.dataset.bugId;
        const card = e.target.closest('.card');
        if (e.target.checked) selected.add(id); else selected.delete(id);
        card.classList.toggle('selected', e.target.checked);
        // sync state across all instances of this card (cluster view duplicates DOM)
        document.querySelectorAll('.card[data-bug-id="' + id + '"]').forEach(c => {
          c.classList.toggle('selected', e.target.checked);
          const x = c.querySelector('.card-select');
          if (x) x.checked = e.target.checked;
        });
        refreshBulkbar();
      };
    });
  }
})();
"""


def _render_board_html(defects: list[dict], banner: str = "") -> str:
    """Render the full triage page HTML."""
    # Feature filter options — gather unique features from defects.
    unique_features: dict[int, str] = {}
    for b in defects:
        if b.get("featureId"):
            unique_features[b["featureId"]] = b.get("featureTitle") or str(b["featureId"])
    feature_opts = '<option value="">All Features</option>' + "".join(
        f'<option value="{fid}">{fid}: {_esc(t[:60])}</option>'
        for fid, t in sorted(unique_features.items(), key=lambda x: x[1].lower())
    )

    state_opts = "".join(f'<option value="{_esc(s)}">{_esc(s)}</option>' for s in STATE_OPTIONS)
    columns_html = _render_columns(defects)
    banner_html = banner if banner else ""

    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>UAT Defect Triage — North Star</title>
<style>{_BOARD_CSS}</style>
</head><body>

<div class="topbar">
  <h1>UAT Defect Triage</h1>
  <div class="grow"></div>
  <a href="/submit">+ Report a defect</a>
  <a href="/triage" title="Reload from ADO">↻ Refresh</a>
</div>

{banner_html}

<div class="filters">
  <input type="search" id="search" placeholder="Search title or ID...">
  <div class="sev-chips" title="Filter by severity">
    <span class="sev-chip" data-sev="1 - Critical">Critical</span>
    <span class="sev-chip" data-sev="2 - High">High</span>
    <span class="sev-chip" data-sev="3 - Medium">Medium</span>
    <span class="sev-chip" data-sev="4 - Low">Low</span>
  </div>
  <select id="feature-filter">{feature_opts}</select>
  <label class="tog"><input type="checkbox" id="show-removed"> Show Removed</label>
  <label class="tog"><input type="checkbox" id="cluster-lens"> Group by cluster</label>
</div>

<div class="bulkbar">
  <span class="count"><strong id="bulk-count">0</strong> selected</span>
  <select id="bulk-state">
    <option value="">Move to...</option>
    {state_opts}
  </select>
  <button id="bulk-apply" class="primary">Apply state change</button>
  <button id="bulk-dup">Mark as duplicate of...</button>
  <button id="bulk-clear" class="danger">Clear</button>
</div>

<div class="board" id="board">
  {columns_html}
</div>
<div id="cluster-container" style="display:none"></div>

<div class="modal-bg" id="dup-modal">
  <div class="modal">
    <h2>Mark as duplicate</h2>
    <p>Pick the canonical Bug — the one to keep open. The others will be linked
       as <em>Duplicate of #canonical</em> and moved to <strong>Done</strong>.</p>
    <label>Canonical Bug
      <select id="canonical-select"></select>
    </label>
    <div class="actions">
      <button id="dup-cancel" class="secondary">Cancel</button>
      <button id="dup-confirm" class="primary">Confirm</button>
    </div>
  </div>
</div>

<script>{_BOARD_JS}</script>
</body></html>"""


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def triage_board() -> HTMLResponse:
    try:
        defects = list_uat_defects()
    except Exception as e:
        log.exception("list_uat_defects failed")
        return HTMLResponse(_render_board_html(
            [],
            banner=f'<div class="banner err"><b>Could not load defects from ADO.</b> {_esc(str(e))[:300]}</div>',
        ))
    return HTMLResponse(_render_board_html(defects))


@router.post("/bulk-state", response_class=HTMLResponse)
async def triage_bulk_state(
    bug_ids: list[str] = Form(default_factory=list),
    new_state: str = Form(...),
) -> HTMLResponse:
    if new_state not in STATE_OPTIONS:
        log.warning("Rejected unknown state: %s", new_state)
        return RedirectResponse(url="/triage", status_code=303)
    ids: list[int] = []
    for b in bug_ids:
        try:
            ids.append(int(b))
        except (TypeError, ValueError):
            continue
    if not ids:
        return RedirectResponse(url="/triage", status_code=303)

    log.info("Triage bulk-state | bugs=%s | new_state=%s", ids, new_state)
    results = bulk_update_state(ids, new_state)
    ok = sum(1 for r in results if r["ok"])
    failed = [r for r in results if not r["ok"]]
    log.info(
        "Triage bulk-state result | ok=%d | failed=%d | failures=%s",
        ok, len(failed), [f"#{r['id']}: {r['error'][:80]}" for r in failed][:5],
    )
    return RedirectResponse(url="/triage", status_code=303)


@router.post("/mark-duplicates", response_class=HTMLResponse)
async def triage_mark_duplicates(
    canonical_id: str = Form(...),
    duplicate_ids: list[str] = Form(default_factory=list),
) -> HTMLResponse:
    try:
        canonical = int(canonical_id)
    except (TypeError, ValueError):
        return RedirectResponse(url="/triage", status_code=303)
    dups: list[int] = []
    for b in duplicate_ids:
        try:
            i = int(b)
            if i != canonical:
                dups.append(i)
        except (TypeError, ValueError):
            continue
    if not dups:
        return RedirectResponse(url="/triage", status_code=303)

    log.info("Triage mark-duplicates | canonical=%d | duplicates=%s", canonical, dups)
    results = mark_as_duplicates(canonical, dups, close_state="Done")
    ok = sum(1 for r in results if r["ok"])
    log.info(
        "Triage mark-duplicates result | canonical=%d | ok=%d | failed=%d",
        canonical, ok, len(results) - ok,
    )
    return RedirectResponse(url="/triage", status_code=303)
