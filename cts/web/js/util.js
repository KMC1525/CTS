// Small DOM + formatting helpers. All text is inserted with textContent (never innerHTML),
// because page text and volume names come from models and user files.

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const SVG_NS = 'http://www.w3.org/2000/svg';

export function icon(name, extraClass = '') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', `icon ${extraClass}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

/** h('div', {class: 'x', onclick: fn}, 'text', childNode) */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value == null) continue;
    if (key === 'class') el.className = value;
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function fmtBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  const v = n / 1024 ** i;
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

export function fmtDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

export function fmtContext(n) {
  if (n >= 1048576) return `${(n / 1048576).toFixed(n % 1048576 ? 1 : 0)}M tokens`;
  return `${Math.round(n / 1024)}K (${n.toLocaleString()})`;
}

export const naturalCompare = (a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });

export function toast(message, kind = '', ms = 5200) {
  const el = h('div', { class: `toast ${kind}`, role: 'status' }, message);
  $('#toasts').append(el);
  setTimeout(() => el.remove(), ms);
}

export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export function paragraphs(text) {
  return String(text || '').split(/\n\s*\n/).map((p) => p.replace(/\s*\n\s*/g, ' ').trim()).filter(Boolean);
}
