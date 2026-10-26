import { api, apiWithKey, isAdmin, friendly } from '../api.js';
import { $, $$, esc, n0, ago, dFull, badge, callout, icon, toast, sleep, skRows, emptyState } from '../util.js';
import { state } from '../data.js';
import { head, settingsTabs } from '../ui.js';

const EVENT_TYPES = [['admission', 'Admission'], ['discharge', 'Discharge'], ['transfer', 'Transfer in'], ['icu_admit', 'ICU admission'], ['icu_discharge', 'ICU discharge'], ['surgery', 'Surgery']];
const SOURCES = [['emergency', 'Emergency'], ['referral', 'Referral'], ['transfer', 'Transfer'], ['scheduled', 'Scheduled']];
const CSV_HEADER = 'patient_ref,event_type,department_id,event_time,severity,admission_source,bed_id,length_of_stay_hours';
let key = ''; // held in memory only: never stored, gone on refresh
let mode = 'single';

const problems = (e) => (e.details?.length ? `<ul style="margin:.4rem 0 0;padding-left:1.1rem">${e.details.map((d) => `<li>${esc(d.field || 'request')}: ${esc(d.message)}</li>`).join('')}</ul>` : '');
const keyError = (e) => (e.status === 401 ? 'That API key was not accepted. Check it, or ask an administrator for a new one.' : e.status === 429 ? 'This key is sending too fast. Wait a moment and try again.' : friendly(e));

export default {
  async render(root, ctx) {
    const nowLocal = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    root.innerHTML = settingsTabs('ingestion') + head('Integrations', 'Send hospital events into the system, then refresh the analysis so they show up in the results.') + `
      <ol class="steps">
        <li><div><h3>Connect your event source</h3><p class="lead">Systems send events with an API key rather than a personal login, so a key can be revoked without affecting anyone's account.</p></div>
          <div class="body"><div class="form"><label class="field" style="min-width:min(26rem,100%)"><span>API key</span><input type="password" id="ig-key" autocomplete="off" spellcheck="false" placeholder="hea_…" value="${esc(key)}"></label>
          <span class="slate" style="font-size:12.5px;padding-bottom:.5rem">Kept in this tab only. It is never saved.</span></div></div></li>
        <li><div><h3>Send events</h3><p class="lead">One at a time to try things out, or a whole file for a bulk load.</p></div>
          <div class="body"><div class="seg" id="ig-mode" role="group" aria-label="How to send"><button type="button" data-v="single" aria-pressed="${mode === 'single'}">Single event</button><button type="button" data-v="bulk" aria-pressed="${mode === 'bulk'}">Bulk file</button></div>
            <div id="ig-single" style="margin-top:1.4rem" ${mode === 'single' ? '' : 'hidden'}>
              <form class="form grid" id="ig-form">
                <label class="field"><span>Patient reference</span><input type="text" id="ig-patient" required maxlength="32" pattern="[A-Za-z0-9_\\-]+" value="p_demo_${Math.floor(Math.random() * 9000 + 1000)}"></label>
                <label class="field"><span>Event</span><select id="ig-type">${EVENT_TYPES.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select></label>
                <label class="field"><span>Department</span><select id="ig-dept">${state.depts.map((d) => `<option value="${d.department_id}">${esc(d.name)}</option>`).join('')}</select></label>
                <label class="field"><span>When (your local time)</span><input type="datetime-local" id="ig-time" value="${nowLocal}" required></label>
                <label class="field"><span>Severity (1 low – 5 critical)</span><input type="number" id="ig-sev" min="1" max="5" value="3" required></label>
                <label class="field"><span>Arrived via</span><select id="ig-src">${SOURCES.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select></label>
                <label class="field"><span>Bed (optional)</span><input type="text" id="ig-bed" maxlength="16" placeholder="C-212"></label>
                <label class="field" id="ig-los-wrap" hidden><span>Length of stay, hours</span><input type="number" id="ig-los" min="0" step="0.1" value="48"></label>
                <div style="align-self:end"><button class="btn primary" type="submit" id="ig-send">Send event</button></div>
              </form><div id="ig-out" style="margin-top:1rem" aria-live="polite"></div></div>
            <div id="ig-bulk" style="margin-top:1.4rem" ${mode === 'bulk' ? '' : 'hidden'}>
              <p class="slate" style="max-width:40rem">Upload your CSV to the hospital's object storage first (for example with <span class="mono" style="font-family:var(--mono);font-size:12.5px">scripts.load_csv --via-storage</span>), then paste its location here. The file is loaded all-or-nothing: if any row is invalid, none are added.</p>
              <div class="code" style="margin:.9rem 0">${esc(CSV_HEADER)}</div>
              <form class="form" id="ig-bform"><label class="field" style="min-width:min(28rem,100%)"><span>File location</span><input type="text" id="ig-uri" required placeholder="s3://hospital-events/uploads/events.csv"></label><button class="btn primary" type="submit" id="ig-bgo">Start load</button></form>
              <div id="ig-bout" style="margin-top:1rem" aria-live="polite"></div></div></div></li>
        <li id="ig-check" ${isAdmin() ? '' : 'hidden'}><div><h3>Check what arrived</h3><p class="lead">Recent loads and the live event rate. New events appear in results after you run an analysis.</p></div>
          <div class="body"><div id="ig-recent">${skRows(3)}</div><p style="margin-top:1rem"><a class="btn" href="#/jobs">Run an analysis</a></p></div></li>
      </ol>
      <section class="section" id="ig-keys" style="margin-top:3rem" ${isAdmin() ? '' : 'hidden'}><hr class="rule" style="margin-top:0">
        <div class="sec-head" style="margin-top:2.75rem"><h2>API keys</h2><p>Create and revoke keys for event sources.</p></div>
        <form class="form" id="ig-kform"><label class="field"><span>Name</span><input type="text" id="ig-kname" required maxlength="64" placeholder="e.g. Ward 4 admissions feed"></label>
          <label class="field"><span>Requests per minute</span><input type="number" id="ig-krate" min="1" value="6000" style="width:9rem"></label><button class="btn" type="submit">${icon('key')} Create key</button></form>
        <div id="ig-knew" style="margin-top:1rem" aria-live="polite"></div><div class="tbl-wrap" style="margin-top:1.4rem" id="ig-klist">${skRows(2)}</div></section>`;

    /* — connect + mode — */
    const keyEl = $('#ig-key', root);
    keyEl.addEventListener('input', () => { key = keyEl.value.trim(); });
    $('#ig-mode', root).addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return; mode = b.dataset.v;
      $$('#ig-mode button', root).forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
      $('#ig-single', root).hidden = mode !== 'single'; $('#ig-bulk', root).hidden = mode !== 'bulk';
    });
    const needKey = (out) => { if (key) return false; out.innerHTML = callout('warn', 'Paste an API key in step 1 first.'); keyEl.focus(); return true; };
    const losWrap = $('#ig-los-wrap', root);
    $('#ig-type', root).addEventListener('change', (e) => { losWrap.hidden = !['discharge', 'icu_discharge'].includes(e.target.value); });

    /* — single event — */
    $('#ig-form', root).addEventListener('submit', async (e) => {
      e.preventDefault();
      const out = $('#ig-out', root), btn = $('#ig-send', root);
      if (needKey(out)) return;
      btn.disabled = true; out.innerHTML = '';
      try {
        const type = $('#ig-type', root).value;
        const body = { patient_ref: $('#ig-patient', root).value.trim(), event_type: type, department_id: +$('#ig-dept', root).value, event_time: new Date($('#ig-time', root).value).toISOString(), severity: +$('#ig-sev', root).value, admission_source: $('#ig-src', root).value };
        if ($('#ig-bed', root).value.trim()) body.bed_id = $('#ig-bed', root).value.trim();
        if (['discharge', 'icu_discharge'].includes(type)) body.length_of_stay_hours = +$('#ig-los', root).value;
        const r = await apiWithKey('/events', key, { method: 'POST', body });
        out.innerHTML = callout('good', `Event <b>#${r.event_id}</b> recorded. Reference for support: <span style="font-family:var(--mono);font-size:12.5px">${esc(r.correlation_id)}</span>`);
      } catch (err) { out.innerHTML = callout('bad', `${esc(keyError(err))}${problems(err)}`); }
      finally { btn.disabled = false; }
    });

    /* — bulk load — */
    $('#ig-bform', root).addEventListener('submit', async (e) => {
      e.preventDefault();
      const out = $('#ig-bout', root), btn = $('#ig-bgo', root);
      if (needKey(out)) return;
      btn.disabled = true; out.innerHTML = callout('info', 'Starting the load…');
      try {
        const a = await apiWithKey('/events/batch', key, { method: 'POST', body: { source_uri: $('#ig-uri', root).value.trim() } });
        for (let i = 0; i < 400; i++) {
          if (!ctx.current()) return;
          const s = await apiWithKey(`/events/batch/${a.batch_id}`, key);
          if (s.status === 'completed') { out.innerHTML = callout('good', `Loaded <b>${n0(s.row_count)} events</b>${s.rows_per_second ? ` (${n0(s.rows_per_second)} per second)` : ''}. Run an analysis to include them in the results.`); break; }
          if (s.status === 'failed') { out.innerHTML = callout('bad', `<b>Nothing was added.</b> ${esc(s.error_detail?.message?.split('\n')[0] ?? 'The file could not be loaded.')}`); break; }
          out.innerHTML = callout('info', s.status === 'loading' ? 'Loading the file…' : 'Waiting for a worker…'); await sleep(1000);
        }
      } catch (err) { out.innerHTML = callout('bad', esc(keyError(err))); }
      finally { btn.disabled = false; }
    });

    /* — admin: recent loads + keys — */
    if (!isAdmin()) return;
    const loadRecent = async () => {
      try {
        const m = await api('/metrics');
        if (!ctx.current()) return;
        const rate = m.M2_ingestion_throughput.single_events, loads = m.M2_ingestion_throughput.bulk_batches;
        $('#ig-recent', root).innerHTML = `<p style="margin-bottom:.8rem"><b class="num">${rate.available ? n0(rate.events) : '—'}</b> <span class="slate">single events in the last 5 minutes</span></p>
          ${loads.length ? `<table class="tbl"><thead><tr><th>Recent bulk loads</th><th class="r">Events</th><th class="r">Time</th><th class="r">Speed</th></tr></thead><tbody>${loads.slice(0, 5).map((l) => `<tr><td class="mono" style="font-family:var(--mono);font-size:12.5px">${esc(l.batch_id.slice(0, 8))}</td><td class="r num">${n0(l.row_count)}</td><td class="r num">${l.seconds ?? '—'} s</td><td class="r num">${l.rows_per_second ? n0(l.rows_per_second) + '/s' : '—'}</td></tr>`).join('')}</tbody></table>` : '<p class="slate">No bulk loads recorded yet.</p>'}`;
      } catch (err) { $('#ig-recent', root).innerHTML = callout('bad', esc(friendly(err))); }
    };
    const loadKeys = async () => {
      try {
        const keys = await api('/admin/api-keys');
        if (!ctx.current()) return;
        $('#ig-klist', root).innerHTML = keys.length ? `<table class="tbl"><thead><tr><th>Name</th><th>Key</th><th>Created</th><th>Last used</th><th>Status</th><th aria-hidden="true"></th></tr></thead><tbody>${keys.map((k) => `<tr><td class="strong">${esc(k.name)}<span class="sub">${n0(k.rate_limit_per_min)} requests/min</span></td><td class="mono" style="font-family:var(--mono);font-size:12.5px">${esc(k.key_prefix)}…</td><td class="slate">${esc(dFull(k.created_at))}</td><td class="slate">${k.last_used_at ? esc(ago(k.last_used_at)) : 'never'}</td><td>${k.is_active ? badge('ok', 'Active') : badge('mod', 'Revoked')}</td><td class="r">${k.is_active ? `<button class="btn sm danger" data-revoke="${k.key_id}">Revoke</button>` : ''}</td></tr>`).join('')}</tbody></table>` : emptyState({ title: 'No keys yet', text: 'Create a key to let a system send events.', ico: 'key', small: true });
        $$('[data-revoke]', root).forEach((b) => b.addEventListener('click', async () => {
          if (!confirm('Revoke this key? Systems using it will stop being able to send events.')) return;
          try { await api(`/admin/api-keys/${b.dataset.revoke}`, { method: 'DELETE' }); toast('Key revoked'); loadKeys(); } catch (err) { toast(friendly(err)); }
        }));
      } catch (err) { $('#ig-klist', root).innerHTML = callout('bad', esc(friendly(err))); }
    };
    $('#ig-kform', root).addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        const k = await api('/admin/api-keys', { method: 'POST', body: { name: $('#ig-kname', root).value.trim(), rate_limit_per_min: +$('#ig-krate', root).value } });
        $('#ig-knew', root).innerHTML = callout('good', `<b>Copy this key now. It is shown only once.</b><br><span style="font-family:var(--mono);font-size:12.5px;user-select:all;overflow-wrap:anywhere">${esc(k.api_key)}</span><br><button class="btn sm" type="button" id="ig-copy" style="margin-top:.5rem">${icon('copy', 14)} Copy</button> <button class="btn sm" type="button" id="ig-use" style="margin-top:.5rem">Use it in step 1</button>`);
        $('#ig-copy', root).addEventListener('click', () => { navigator.clipboard?.writeText(k.api_key); toast('Copied'); });
        $('#ig-use', root).addEventListener('click', () => { key = k.api_key; keyEl.value = key; toast('Key added to step 1'); });
        $('#ig-kname', root).value = ''; loadKeys();
      } catch (err) { $('#ig-knew', root).innerHTML = callout('bad', `${esc(friendly(err))}${problems(err)}`); }
    });
    loadRecent(); loadKeys();
  },
};
