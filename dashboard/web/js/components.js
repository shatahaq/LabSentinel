/* Reusable UI pieces: toasts, dialogs, empty states.
 *
 * Dialogs trap focus and close on Escape; destructive and mass actions always
 * go through confirm() so nothing surprising happens to 40 machines at once.
 */
(function (global) {
  "use strict";

  const { $, esc, icon } = global.U;

  /* ---------------------------------------------------------------- toasts */
  const TOAST_ICON = { ok: "i-check", error: "i-alert", warn: "i-alert", info: "i-message" };

  // A burst of violations must not bury the screen; oldest ones give way.
  const MAX_TOASTS = 4;

  function toast(title, text, kind) {
    const type = kind || "ok";
    const node = global.U.el(`
      <div class="toast toast--${esc(type)}">
        <svg class="toast__icon" aria-hidden="true"><use href="#${TOAST_ICON[type] || "i-check"}"/></svg>
        <div class="toast__body">
          <div class="toast__title">${esc(title)}</div>
          ${text ? `<div class="toast__text">${esc(text)}</div>` : ""}
        </div>
        <button class="btn btn--icon btn--sm btn--ghost" aria-label="Tutup pemberitahuan">
          ${icon("x", "btn__icon")}
        </button>
      </div>`);
    const remove = () => {
      node.style.opacity = "0";
      setTimeout(() => node.remove(), 140);
    };
    node.querySelector("button").addEventListener("click", remove);
    const host = $("#toasts");
    host.appendChild(node);
    while (host.children.length > MAX_TOASTS) host.firstElementChild.remove();
    setTimeout(remove, type === "error" ? 7000 : 4000);
    return node;
  }

  /** Report the outcome of a command across many PCs, honestly. */
  function reportToast(result, verb) {
    if (!result.ok) {
      toast("Gagal", result.error, "error");
      return;
    }
    const failed = (result.failed || []).length;
    if (failed === 0) {
      toast(`${verb} berhasil`, result.summary, "ok");
    } else {
      toast(
        `${verb} sebagian berhasil`,
        `${result.summary}. Tidak berhasil: ${result.failed.join(", ")}.`,
        "warn"
      );
    }
  }

  /* --------------------------------------------------------------- dialogs */
  let lastFocused = null;

  function closeDialog() {
    const scrim = $("#scrim");
    scrim.hidden = true;
    scrim.innerHTML = "";
    document.removeEventListener("keydown", onKeydown, true);
    if (lastFocused && lastFocused.focus) lastFocused.focus();
    lastFocused = null;
  }

  function onKeydown(ev) {
    if (ev.key === "Escape") {
      ev.stopPropagation();
      closeDialog();
      return;
    }
    if (ev.key !== "Tab") return;
    const focusables = Array.from(
      $("#scrim").querySelectorAll(
        'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      )
    ).filter((n) => n.offsetParent !== null);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  }

  /** Open a dialog. `build(body, close)` fills the body element. */
  function openDialog({ title, subtitle, wide, build, footer }) {
    lastFocused = document.activeElement;
    const scrim = $("#scrim");
    const node = global.U.el(`
      <div class="dialog ${wide ? "dialog--wide" : ""}" role="dialog" aria-modal="true"
           aria-label="${esc(title)}">
        <div class="dialog__head">
          <div>
            <h2 class="dialog__title">${esc(title)}</h2>
            ${subtitle ? `<p class="dialog__sub">${esc(subtitle)}</p>` : ""}
          </div>
          <button class="btn btn--icon btn--ghost" style="margin-left:auto"
                  aria-label="Tutup">${icon("x", "btn__icon")}</button>
        </div>
        <div class="dialog__body"></div>
      </div>`);

    node.querySelector(".dialog__head button").addEventListener("click", closeDialog);
    const body = node.querySelector(".dialog__body");
    if (build) build(body, closeDialog);

    if (footer) {
      const foot = global.U.el('<div class="dialog__foot"></div>');
      footer(foot, closeDialog);
      node.appendChild(foot);
    }

    scrim.innerHTML = "";
    scrim.appendChild(node);
    scrim.hidden = false;
    scrim.onclick = (ev) => { if (ev.target === scrim) closeDialog(); };
    document.addEventListener("keydown", onKeydown, true);

    const focusTarget = node.querySelector("[data-autofocus]") || node.querySelector("button");
    if (focusTarget) focusTarget.focus();
    return { close: closeDialog, node };
  }

  /**
   * Confirmation for anything hard to undo. Returns a Promise<boolean>.
   * `danger` styles the confirm button and adds a warning strip.
   */
  function confirm({ title, message, confirmLabel, danger, warning }) {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        resolve(value);
      };

      openDialog({
        title,
        build: (body) => {
          body.innerHTML = `
            <p style="font-size:var(--text-md)">${esc(message)}</p>
            ${warning ? `<div class="dialog__warn" style="margin-top:var(--s-4)">
                ${icon("alert")}<div>${esc(warning)}</div></div>` : ""}`;
        },
        footer: (foot, close) => {
          const cancel = global.U.el('<button class="btn">Batal</button>');
          const ok = global.U.el(
            `<button class="btn ${danger ? "btn--danger" : "btn--primary"}" data-autofocus>
               ${esc(confirmLabel || "Lanjutkan")}</button>`
          );
          cancel.addEventListener("click", () => { finish(false); close(); });
          ok.addEventListener("click", () => { finish(true); close(); });
          foot.append(cancel, ok);
        },
      });

      // Closing by Escape / backdrop counts as "no".
      const scrim = $("#scrim");
      const observer = new MutationObserver(() => {
        if (scrim.hidden) {
          observer.disconnect();
          finish(false);
        }
      });
      observer.observe(scrim, { attributes: true, attributeFilter: ["hidden"] });
    });
  }

  /* ----------------------------------------------------------- empty state */
  function empty({ iconName, title, text, actionLabel, onAction }) {
    const node = global.U.el(`
      <div class="empty">
        <svg class="empty__icon" aria-hidden="true"><use href="#i-${esc(iconName || "monitor")}"/></svg>
        <div class="empty__title">${esc(title)}</div>
        <p class="empty__text">${esc(text || "")}</p>
      </div>`);
    if (actionLabel && onAction) {
      const btn = global.U.el(
        `<button class="btn btn--primary" style="margin-top:var(--s-2)">${esc(actionLabel)}</button>`
      );
      btn.addEventListener("click", onAction);
      node.appendChild(btn);
    }
    return node;
  }

  /** Run an async action while showing the button as busy. */
  async function withBusy(button, fn) {
    if (!button) return fn();
    const previous = button.getAttribute("aria-busy");
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
    try {
      return await fn();
    } finally {
      if (previous) button.setAttribute("aria-busy", previous);
      else button.removeAttribute("aria-busy");
      button.disabled = false;
    }
  }

  global.C = { toast, reportToast, openDialog, closeDialog, confirm, empty, withBusy };
})(window);
