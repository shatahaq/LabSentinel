/* Small helpers: DOM, escaping, Indonesian time formatting, status mapping. */
(function (global) {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  /** Escape text before it goes anywhere near innerHTML. */
  function esc(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function el(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  function icon(name, cls) {
    return `<svg class="${cls || ""}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  }

  /* ---- time ---- */
  const TIME_FMT = new Intl.DateTimeFormat("id-ID", { hour: "2-digit", minute: "2-digit" });
  const DATETIME_FMT = new Intl.DateTimeFormat("id-ID", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
  const FULL_FMT = new Intl.DateTimeFormat("id-ID", {
    weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit",
  });

  function clock(ts) { return ts ? TIME_FMT.format(new Date(ts * 1000)) : "—"; }
  function datetime(ts) { return ts ? DATETIME_FMT.format(new Date(ts * 1000)) : "—"; }
  function fullDate(ts) { return ts ? FULL_FMT.format(new Date(ts * 1000)) : "—"; }

  /** "baru saja" / "3 menit lalu" — relative time in plain Indonesian. */
  function ago(ts) {
    if (!ts) return "—";
    const secs = Math.max(0, Date.now() / 1000 - ts);
    if (secs < 10) return "baru saja";
    if (secs < 60) return `${Math.floor(secs)} detik lalu`;
    if (secs < 3600) return `${Math.floor(secs / 60)} menit lalu`;
    if (secs < 86400) return `${Math.floor(secs / 3600)} jam lalu`;
    return datetime(ts);
  }

  function duration(secs) {
    if (!secs || secs < 0) return "—";
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    if (h > 0) return `${h} jam ${m} menit`;
    if (m > 0) return `${m} menit`;
    return `${Math.floor(secs)} detik`;
  }

  /* ---- status ----
   * Each status carries an icon and a word, so it is never colour-only. */
  const STATUS = {
    online:  { label: "Aktif",    icon: "i-dot",        cls: "status--online" },
    paused:  { label: "Dijeda",   icon: "i-pause-filled", cls: "status--paused" },
    dropped: { label: "Terputus", icon: "i-alert-filled", cls: "status--dropped" },
    offline: { label: "Mati",     icon: "i-dot-hollow", cls: "status--offline" },
  };

  function statusInfo(key) { return STATUS[key] || STATUS.offline; }

  function statusChip(key) {
    const s = statusInfo(key);
    return `<span class="status ${s.cls}">
      <svg class="status__icon" aria-hidden="true"><use href="#${s.icon}"/></svg>${s.label}</span>`;
  }

  /* ---- violations ---- */
  const VIOLATION_LABEL = {
    app_blocked: "Aplikasi tidak diizinkan",
    app_closed: "Aplikasi ditutup",
    impersonation: "Aplikasi menyamar",
    site_title: "Dugaan situs terlarang",
    agent_tamper: "Agent dihentikan tidak wajar",
  };

  function violationLabel(kind) { return VIOLATION_LABEL[kind] || kind; }

  function debounce(fn, ms) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  function plural(n, word) { return `${n} ${word}`; }

  global.U = {
    $, $$, esc, el, icon, clock, datetime, fullDate, ago, duration,
    statusInfo, statusChip, violationLabel, debounce, plural,
  };
})(window);
