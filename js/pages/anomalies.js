import { friendly } from '../api.js';
import { $, $$, esc, n0, badge, dFull, dDayTime, SEVERITY, METRIC, skRows, skMetrics, emptyState, callout, icon } from '../util.js';
import { state, currentWindow, getAnomalies, deptName, countBySeverity, deptBeds, occupied } from '../data.js';
import { head, filterTools, bindFilters, describe, rangeText } from '../ui.js';

const bedFacts = (a, rate) => { const cap = deptBeds(a.department_id); return `${cap ? `${occupied(rate, cap)} of ${cap} beds · ` : ''}${(rate * 100).toFixed(1)}%`; };
const local = { severity: 'all', metric: '', direction: '', shown: 20 };

export default {
  async render(root, ctx) {
    if (!state.range) { root.innerHTML = head('Unusual workloads', '') + emptyState({ title: 'No results yet', text: 'Run an analysis to detect unusual workloads.', action: '<a class="btn primary" href="#/jobs">Run an analysis</a>' }); return; }
    const w = currentWindow();
    root.innerHTML = head('Unusual workloads', `Days and hours that stood out from what is normal for that department, weekday and time, in either direction, between <b>${rangeText(w)}</b>.`, filterTools()) + `
      <section class="section" id="an-metrics">${skMetrics()}</section>
      <section class="section">
        <div class="sec-head" style="align-items:center"><div class="seg" role="group" aria-label="Severity" id="an-sev">
          ${[['all', 'All'], ['critical', 'Critical'], ['high', 'High'], ['moderate', 'Moderate']].map(([k, l]) => `<button type="button" data-v="${k}" aria-pressed="${local.severity === k}">${l}</button>`).join('')}</div>
          <div class="tools"><label class="sr" for="an-metric">Measure</label><select id="an-metric"><option value="">All measures</option>${Object.entries(METRIC).map(([k, l]) => `<option value="${k}"${local.metric === k ? ' selected' : ''}>${l}</option>`).join('')}</select>
          <label class="sr" for="an-dir">Direction</label><select id="an-dir"><option value="">Higher or lower</option><option value="high"${local.direction === 'high' ? ' selected' : ''}>Higher than usual</option><option value="low"${local.direction === 'low' ? ' selected' : ''}>Lower than usual</option></select></div></div>
        <div class="tbl-wrap" id="an-list">${skRows(8)}</div>
        <p style="margin-top:1rem"><button class="btn" id="an-more" hidden></button></p>
      </section>`;
    bindFilters(root, ctx.rerender);

    let all;
    try { all = await getAnomalies(w); } catch (e) { if (ctx.current()) $('#an-list', root).innerHTML = callout('bad', `We could not load unusual workloads. ${esc(friendly(e))}`); return; }
    if (!ctx.current()) return;

    const c = countBySeverity(all);
    $('#an-metrics', root).innerHTML = `<div class="metrics">
      <div class="metric"><p class="k">Unusual periods</p><p class="v num">${n0(all.length)}</p><p class="d">in this time frame</p></div>
      <div class="metric"><p class="k">Critical</p><p class="v num" style="color:${c.critical ? 'var(--red)' : 'inherit'}">${n0(c.critical)}</p><p class="d">need attention first</p></div>
      <div class="metric"><p class="k">High</p><p class="v num">${n0(c.high)}</p><p class="d">worth reviewing</p></div>
      <div class="metric"><p class="k">Moderate</p><p class="v num">${n0(c.moderate)}</p><p class="d">for context</p></div></div>`;

    const draw = () => {
      const list = all
        .filter((a) => (local.severity === 'all' || a.severity_label === local.severity) && (!local.metric || a.metric_name === local.metric) && (!local.direction || a.direction === local.direction))
        .sort((a, b) => SEVERITY[b.severity_label].rank - SEVERITY[a.severity_label].rank || Math.abs(b.z_score) - Math.abs(a.z_score));
      const host = $('#an-list', root), more = $('#an-more', root);
      if (!list.length) { host.innerHTML = emptyState({ title: 'Nothing matches', text: all.length ? 'Try a different filter.' : 'No unusual workload periods in this time frame.', ico: 'check', small: true }); more.hidden = true; return; }
      host.innerHTML = `<table class="tbl"><thead><tr><th>Severity</th><th>Department</th><th>When</th><th>What happened</th><th class="r">vs expected</th><th aria-hidden="true"></th></tr></thead><tbody>${list.slice(0, local.shown).map((a, i) => {
        const d = describe(a);
        return `<tr class="click" tabindex="0" role="button" aria-expanded="false" data-i="${i}">
          <td>${badge(d.sev.cls, d.sev.label)}</td><td class="strong">${esc(deptName(a.department_id))}</td><td>${esc(d.when)}</td>
          <td>${esc(d.metric)} <span class="sub">${esc(d.detail)}</span></td><td class="r num strong">${esc(d.magnitude)}</td><td class="r"><span class="chev">${icon('chevron')}</span></td></tr>`;
      }).join('')}</tbody></table>`;
      const rest = list.length - local.shown;
      more.hidden = rest <= 0; more.textContent = `Show ${Math.min(rest, 20)} more`;
      const toggle = (tr) => {
        const open = tr.getAttribute('aria-expanded') === 'true';
        tr.setAttribute('aria-expanded', String(!open));
        if (open) { tr.nextElementSibling?.classList.contains('detail') && tr.nextElementSibling.remove(); return; }
        const a = list[+tr.dataset.i], d = describe(a);
        tr.insertAdjacentHTML('afterend', `<tr class="detail"><td colspan="6"><dl class="facts">
          <div><dt>Window</dt><dd>${esc(dDayTime(a.window_start))} → ${esc(dDayTime(a.window_end))} UTC</dd></div>
          <div><dt>Observed</dt><dd class="num">${esc(a.metric_name === 'occupancy_rate' ? bedFacts(a, a.observed_value) : a.observed_value.toFixed(1))}</dd></div>
          <div><dt>Expected</dt><dd class="num">${esc(a.metric_name === 'occupancy_rate' ? bedFacts(a, a.expected_value) : a.expected_value.toFixed(1))}</dd></div>
          <div><dt>Why it was flagged</dt><dd>${d.up ? 'Higher' : 'Lower'} than the usual ups and downs for this department, weekday and time of day: ${Math.abs(a.z_score).toFixed(1)}× the normal variation</dd></div>
          <div><dt>Found by analysis</dt><dd class="mono">${esc(a.job_id.slice(0, 8))}</dd></div></dl></td></tr>`);
      };
      $$('tr.click', host).forEach((tr) => {
        tr.addEventListener('click', () => toggle(tr));
        tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(tr); } });
      });
    };
    draw();
    $('#an-sev', root).addEventListener('click', (e) => { const b = e.target.closest('button'); if (!b) return; local.severity = b.dataset.v; local.shown = 20; $$('#an-sev button', root).forEach((x) => x.setAttribute('aria-pressed', String(x === b))); draw(); });
    $('#an-metric', root).addEventListener('change', (e) => { local.metric = e.target.value; local.shown = 20; draw(); });
    $('#an-dir', root).addEventListener('change', (e) => { local.direction = e.target.value; local.shown = 20; draw(); });
    $('#an-more', root).addEventListener('click', () => { local.shown += 20; draw(); });
  },
};
