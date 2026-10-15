/* Shared filters and the data fetchers every page uses. Only existing API endpoints. */
import { api } from './api.js';
import { addDays, diffDays, isoDay, parseDay } from './util.js';

export const state = {
  depts: [],        // [{department_id, name, ...}]
  range: null,      // {first, last} dates that have results, or null
  period: 'year',   // month | quarter | year | all
  dept: '',         // '' = all departments
};

export const PERIODS = [
  ['month', 'Last 30 days', 30],
  ['quarter', 'Last 90 days', 90],
  ['year', 'Last 12 months', 365],
  ['all', 'All time', null],
];
export const periodLabel = (p = state.period) => PERIODS.find((x) => x[0] === p)[1].toLowerCase();

export const deptName = (id) => state.depts.find((d) => d.department_id === +id)?.name ?? `Department ${id}`;
export const deptQuery = (d = state.dept) => (d ? `&department_id=${d}` : '');

/** The selected period as {start, end, days}, clamped to the dates that have results. */
export function currentWindow(period = state.period) {
  const { first, last } = state.range;
  const days = PERIODS.find((x) => x[0] === period)[2];
  const start = days ? isoDay(new Date(Math.max(parseDay(first), addDays(parseDay(last), -(days - 1))))) : first;
  return { start, end: last, days: diffDays(start, last) + 1 };
}
/** The period immediately before `w`, same length; null if it would start before the data does. */
export function previousWindow(w) {
  const end = isoDay(addDays(parseDay(w.start), -1));
  const start = isoDay(addDays(parseDay(end), -(w.days - 1)));
  return start >= state.range.first ? { start, end, days: w.days } : null;
}

export async function loadDepartments() {
  const d = await api('/overview/departments');
  state.depts = d.departments;
  state.range = d.analysed_range;
  return d;
}

export const getSummary = (w, dept = state.dept) => api(`/overview/summary?start_date=${w.start}&end_date=${w.end}${deptQuery(dept)}`);
export const getPattern = (w, dept = state.dept) => api(`/overview/weekly-pattern?start_date=${w.start}&end_date=${w.end}${deptQuery(dept)}`);

export const getAnomalies = (w, dept = state.dept, limit = 1000) =>
  api(`/analytics/anomalies?start=${w.start}T00:00:00Z&end=${isoDay(addDays(parseDay(w.end), 1))}T00:00:00Z&limit=${limit}${deptQuery(dept)}`).then((r) => r.data);

/** Follow cursor pagination to the end (or maxPages). `onPage(rowsSoFar)` reports progress. */
export async function fetchAll(path, { limit = 1000, maxPages = 12, onPage } = {}) {
  const out = [];
  let cursor = null;
  for (let i = 0; i < maxPages; i++) {
    const r = await api(`${path}${path.includes('?') ? '&' : '?'}limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
    out.push(...r.data);
    onPage?.(out.length);
    cursor = r.next_cursor;
    if (!cursor) return { rows: out, complete: true };
  }
  return { rows: out, complete: false };
}

/** Weighted bed utilisation across departments (weights = bed capacity). */
export function bedUse(departments) {
  const d = departments.filter((x) => x.bed_use_avg != null);
  const cap = d.reduce((a, x) => a + x.bed_capacity, 0);
  return cap ? d.reduce((a, x) => a + x.bed_use_avg * x.bed_capacity, 0) / cap : null;
}

export function countBySeverity(list) {
  const c = { critical: 0, high: 0, moderate: 0 };
  for (const a of list) c[a.severity_label] = (c[a.severity_label] ?? 0) + 1;
  return c;
}
