"use strict";

/** 最小 DOM 辅助（浏览器与离线探针共用同一套调用）。 */

export function byId(id) {
  return document.getElementById(id);
}

export function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

export function clear(element) {
  if (!element) return element;
  while (element.firstChild) element.removeChild(element.firstChild);
  return element;
}

export function setHidden(element, hidden) {
  if (!element) return element;
  element.hidden = Boolean(hidden);
  return element;
}
