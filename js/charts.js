/* Chart primitives: dependency-free SVG/HTML. Restrained colour: one blue, one grey, semantic only for status. */
import { $, esc } from './util.js';

export const COLOR = { blue: '#2563EB', grey: '#94A3B8', green: '#3BAA6B', amber: '#E0A030', red: '#D64545' };

export function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const m = v / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
}

/**
 * Line chart with optional area fill, multiple series and a hover tooltip.
 * series: [{name, color, points:[{x:number, y:number|null}], area?, dash?}]
 * o: {yFmt(v, forTip), xFmt(x), tipTitle(x), label, yMin?, yMax?, xTicks?, height?}
 */
export function lineChart(host, series, o) {
  const pts = series.flatMap((s) => s.points.filter((p) => p.y != null));
  if (!pts.length) { host.innerHTML = ''; host._chart = null; return false; }
  // Draw at the real pixel width so text stays 11px at any size; redraw when the column is resized.
  const W = Math.max(320, Math.round(host.clientWidth || 760)), H = o.height ?? 260, L = 44, R = 8, T = 12, B = 28;
  host._chart = { series, o }; host._w = W;
  if (!host._ro && 'ResizeObserver' in window) {
    host._ro = new ResizeObserver(() => { if (host._chart && Math.abs(host.clientWidth - host._w) > 24) lineChart(host, host._chart.series, host._chart.o); });
    host._ro.observe(host);
  }
  const x0 = Math.min(...pts.map((p) => p.x)), x1 = Math.max(...pts.map((p) => p.x));
  const yMin = o.yMin ?? 0;
  const yMax = o.yMax ?? niceMax(Math.max(...pts.map((p) => p.y)) * 1.06);
  const X = (x) => L + (x1 === x0 ? 0.5 : (x - x0) / (x1 - x0)) * (W - L - R);
  const Y = (y) => T + (1 - (y - yMin) / (yMax - yMin)) * (H - T - B);
  const gid = 'g' + Math.random().toString(36).slice(2, 8);

  const grid = [0, 1, 2, 3, 4].map((i) => {
    const v = yMin + ((yMax - yMin) * i) / 4;
    return `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/><text class="tick" x="${L - 8}" y="${Y(v) + 4}" text-anchor="end">${esc(o.yFmt(v))}</text>`;
  }).join('');
  const xt = o.xTicks ?? [0, 1, 2, 3, 4].map((i) => x0 + ((x1 - x0) * i) / 4);
  const xl = xt.map((x, i) => `<text class="tick" x="${X(x)}" y="${H - 6}" text-anchor="${i === 0 ? 'start' : i === xt.length - 1 ? 'end' : 'middle'}">${esc(o.xFmt(x))}</text>`).join('');

  const paths = series.map((s) => {
    let d = '', pen = false;
    for (const p of s.points) {
      if (p.y == null) { pen = false; continue; }
      d += `${pen ? 'L' : 'M'}${X(p.x).toFixed(1)} ${Y(p.y).toFixed(1)} `;
      pen = true;
    }
    const valid = s.points.filter((p) => p.y != null);
    const area = s.area && valid.length > 1 ? `<path d="${d}L${X(valid.at(-1).x)} ${Y(yMin)} L${X(valid[0].x)} ${Y(yMin)} Z" fill="url(#${gid})"/>` : '';
    return `${area}<path class="line" d="${d}" stroke="${s.color}" ${s.dash ? 'stroke-dasharray="5 5"' : ''}/>`;
  }).join('');

  host.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.label)}">
      <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${COLOR.blue}" stop-opacity=".12"/><stop offset="1" stop-color="${COLOR.blue}" stop-opacity="0"/></linearGradient></defs>
      ${grid}${xl}${paths}
      <line class="guide" y1="${T}" y2="${H - B}" x1="0" x2="0" style="display:none"/><g class="dots"></g>
    </svg><div class="tip" role="status"></div>`;

  const svg = $('svg', host), tip = $('.tip', host), guide = $('.guide', svg), dots = $('.dots', svg);
  const nearest = (s, x) => {
    let best = null, bd = Infinity;
    for (const p of s.points) { if (p.y == null) continue; const d = Math.abs(p.x - x); if (d < bd) { bd = d; best = p; } }
    return best;
  };
  svg.addEventListener('pointermove', (e) => {
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const xv = x0 + ((px - L) / (W - L - R)) * (x1 - x0);
    const hits = series.map((s) => ({ s, p: nearest(s, xv) })).filter((h) => h.p);
    if (!hits.length) return;
    const lead = hits[0].p;
    guide.setAttribute('x1', X(lead.x)); guide.setAttribute('x2', X(lead.x)); guide.style.display = '';
    dots.innerHTML = hits.map((h) => `<circle cx="${X(h.p.x)}" cy="${Y(h.p.y)}" r="3.5" fill="#fff" stroke="${h.s.color}" stroke-width="2"/>`).join('');
    tip.innerHTML = `<b>${esc(o.tipTitle(lead.x))}</b>${hits.map((h) => `<span class="muted">${series.length > 1 ? esc(h.s.name) + ' ' : ''}</span>${esc(o.yFmt(h.p.y, true))}`).join('<br>')}`;
    tip.style.left = `${Math.min(Math.max((X(lead.x) / W) * 100, 8), 92)}%`;
    tip.style.top = `${(Y(Math.max(...hits.map((h) => h.p.y))) / H) * rect.height}px`;
    tip.classList.add('on');
  });
  svg.addEventListener('pointerleave', () => { tip.classList.remove('on'); guide.style.display = 'none'; dots.innerHTML = ''; });
  return true;
}

/**
 * Rows of "name · bar · value". Each row: {name, width (0-100), value, sub?, tone?, marker? (0-1), ref? (0-1)}.
 * `marker` draws a tick (e.g. the busiest moment); `ref` draws a dashed reference line (e.g. 90% full).
 */
export function rows(ul, items) {
  ul.innerHTML = items.map((r) => `
    <li>
      <span class="nm">${esc(r.name)}</span>
      <span class="track" aria-hidden="true"><i class="${r.tone ?? ''}" style="width:0%" data-w="${r.width}"></i>${r.ref != null ? `<u style="left:${r.ref * 100}%"></u>` : ''}${r.marker != null ? `<em style="left:calc(${Math.min(r.marker, 1) * 100}% - 1px)"></em>` : ''}</span>
      <span class="vl">${r.value}</span>
      ${r.sub ? `<span class="sub">${r.sub}</span>` : ''}
    </li>`).join('');
  requestAnimationFrame(() => ul.querySelectorAll('i[data-w]').forEach((i) => { i.style.width = `${i.dataset.w}%`; }));
}

/** Stacked share bar with legend. parts: [{label, value, color}] */
export function stack(host, parts) {
  const total = parts.reduce((a, p) => a + p.value, 0);
  if (!total) { host.innerHTML = ''; return; }
  host.innerHTML = `
    <div class="stack" role="img" aria-label="${esc(parts.map((p) => `${p.label} ${Math.round((p.value / total) * 100)}%`).join(', '))}">
      ${parts.map((p) => `<i style="width:${(p.value / total) * 100}%;background:${p.color}"></i>`).join('')}
    </div>
    <div class="legend">${parts.map((p) => `<span><i class="dot" style="background:${p.color}"></i>${esc(p.label)} <b class="num" style="color:var(--ink);font-weight:500">${Math.round((p.value / total) * 100)}%</b></span>`).join('')}</div>`;
}

/** Day × hour heatmap of bed utilisation. cells: Map<'YYYY-MM-DD|h', value 0..n> */
export function heatmap(host, days, cells, fmtDay) {
  const tone = (v) => {
    if (v == null) return '#F1F4F9';
    if (v >= 0.9) return v >= 1 ? '#C62828' : '#E5766B';
    if (v >= 0.75) return '#F0C36B';
    const t = Math.max(0, Math.min(1, (v - 0.25) / 0.5));
    const a = [219, 232, 253], b = [96, 140, 240];
    return `rgb(${a.map((c, i) => Math.round(c + (b[i] - c) * t)).join(',')})`;
  };
  host.innerHTML = `<div class="heat" style="grid-template-columns:auto repeat(24,minmax(0,1fr))" role="img" aria-label="Bed utilisation by day and hour">
    <span></span>${Array.from({ length: 24 }, (_, h) => `<span class="hr">${h % 6 === 0 ? h : ''}</span>`).join('')}
    ${days.map((d) => `<span class="lab">${esc(fmtDay(d))}</span>${Array.from({ length: 24 }, (_, h) => {
      const v = cells.get(`${d}|${h}`);
      return `<span class="cell" style="background:${tone(v)}" title="${esc(fmtDay(d))}, ${h}:00 — ${v == null ? 'no data' : Math.round(v * 100) + '% of beds in use'}"></span>`;
    }).join('')}`).join('')}
  </div>
  <div class="scale">
    <span><i style="background:#DBE8FD"></i>Under 75%</span><span><i style="background:#F0C36B"></i>75–90%</span>
    <span><i style="background:#E5766B"></i>90% or more</span><span><i style="background:#C62828"></i>Over capacity</span>
  </div>`;
}
