/* Small helpers: DOM, formatting (plain words, not codes), icons, loading placeholders. */

export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ── numbers and dates ── */
const nf = new Intl.NumberFormat('en-GB');
export const n0 = (v) => (v == null ? '—' : nf.format(Math.round(v)));
export const n1 = (v) => (v == null ? '—' : nf.format(Math.round(v * 10) / 10));
export const pct = (v, d = 0) => (v == null ? '—' : `${(v * 100).toFixed(d)}%`);
export const signed = (v, suffix = '') => `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v)}${suffix}`;

const fmt = (o) => new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', ...o });
const F = {
  full: fmt({ day: 'numeric', month: 'short', year: 'numeric' }),
  short: fmt({ day: 'numeric', month: 'short' }),
  weekday: fmt({ weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }),
  month: fmt({ month: 'short', year: 'numeric' }),
  time: fmt({ hour: '2-digit', minute: '2-digit', hour12: false }),
  dayTime: fmt({ day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }),
};
export const dFull = (d) => F.full.format(typeof d === 'string' ? new Date(d.length === 10 ? d + 'T00:00:00Z' : d) : d);
export const dShort = (d) => F.short.format(typeof d === 'string' ? new Date(d.length === 10 ? d + 'T00:00:00Z' : d) : d);
export const dWeekday = (d) => F.weekday.format(typeof d === 'string' ? new Date(d.length === 10 ? d + 'T00:00:00Z' : d) : d);
export const dMonth = (d) => F.month.format(d);
export const dTime = (d) => F.time.format(new Date(d));
export const dDayTime = (d) => F.dayTime.format(new Date(d));

export const parseDay = (s) => new Date(s + 'T00:00:00Z');
export const isoDay = (d) => d.toISOString().slice(0, 10);
export const addDays = (d, n) => new Date(d.getTime() + n * 86400000);
export const diffDays = (a, b) => Math.round((parseDay(b) - parseDay(a)) / 86400000);

export function hourWord(h) {
  h = ((Math.round(h) % 24) + 24) % 24;
  return h === 0 ? '12 am' : h === 12 ? '12 pm' : h < 12 ? `${h} am` : `${h - 12} pm`;
}
export function stayWord(hours) {
  if (hours == null) return '—';
  return hours < 48 ? `${Math.max(1, Math.round(hours))} h` : `${(hours / 24).toFixed(1)} d`;
}
export function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return dShort(iso);
}
export function duration(sec) {
  if (sec == null) return '—';
  if (sec < 60) return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)} s`;
  return `${Math.floor(sec / 60)} min ${Math.round(sec % 60)} s`;
}

/* ── labels ── */
export const METRIC = { daily_admissions: 'Daily admissions', hourly_admissions: 'Hourly arrivals', occupancy_rate: 'Bed utilisation' };
export const SEVERITY = { critical: { cls: 'crit', label: 'Critical', rank: 3 }, high: { cls: 'high', label: 'High', rank: 2 }, moderate: { cls: 'mod', label: 'Moderate', rank: 1 } };
export const JOB_TYPE = { full_analysis: 'Full analysis', admission_patterns: 'Admission patterns', resource_utilisation: 'Resource utilisation', anomaly_detection: 'Unusual workload detection' };
export const STATUS = { queued: 'Queued', running: 'Running', completed: 'Completed', partial: 'Partial', failed: 'Failed', cancelled: 'Cancelled' };
export const SOURCE = { emergency: 'Emergency', referral: 'Referral', transfer: 'Transfer', scheduled: 'Scheduled' };
export const badge = (cls, text) => `<span class="badge ${cls}">${esc(text)}</span>`;

/* ── icons (stroke, 16px) ── */
const P = {
  overview: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
  admissions: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  resources: '<path d="M2 4v16"/><path d="M2 8h18a2 2 0 0 1 2 2v10"/><path d="M2 17h20"/><path d="M6 8v9"/>',
  anomalies: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  jobs: '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
  ingestion: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  reports: '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  out: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  key: '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6"/><path d="m15.5 7.5 3 3L22 7l-3-3"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  alert: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
  print: '<path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/>',
  up: '<path d="m18 15-6-6-6 6"/>',
};
export const icon = (name, size = 16) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] ?? ''}</svg>`;

/* ── loading placeholders ── */
export const sk = (w = '100%', h = '1rem', extra = '') => `<span class="sk" style="width:${w};height:${h};${extra}"></span>`;
export const skBlock = (h = '14rem') => `<span class="sk" style="width:100%;height:${h}"></span>`;
export const skRows = (n = 4) => `<div style="display:grid;gap:1rem">${Array.from({ length: n }, () => sk('100%', '1.1rem')).join('')}</div>`;
export const skMetrics = () => `<div class="metrics">${Array.from({ length: 4 }, () => `<div class="metric">${sk('5rem', '0.8rem')}<div style="margin-top:.6rem">${sk('7rem', '1.8rem')}</div><div style="margin-top:.5rem">${sk('6rem', '.8rem')}</div></div>`).join('')}</div>`;

export function emptyState({ title, text, action = '', ico = 'inbox', small = false }) {
  return `<div class="empty${small ? ' small' : ''}">${icon(ico, 28)}<h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
}
export const callout = (kind, text) => `<div class="callout ${kind}" role="${kind === 'bad' ? 'alert' : 'status'}">${icon(kind === 'good' ? 'check' : kind === 'bad' || kind === 'warn' ? 'alert' : 'info')}<p>${text}</p></div>`;

export function toast(text) {
  const t = document.createElement('div');
  t.className = 'toast'; t.setAttribute('role', 'status'); t.innerHTML = `${icon('check')}<span>${esc(text)}</span>`;
  document.body.append(t);
  setTimeout(() => t.remove(), 2800);
}
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
