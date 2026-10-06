/* App wiring: routing, live updates, theme, onboarding.
 *
 * Live updates arrive by polling the bridge's event buffer. Polling (rather
 * than pushing into gevent from the asyncio thread) keeps the threading model
 * simple and means a frozen UI can never back up memory on the server.
 */
(function (global) {
  "use strict";

  const { $, $$ } = global.U;
  const { toast, reportToast, confirm, withBusy, openDialog } = global.C;
  const P = global.Pages;

  let currentPage = "beranda";
  let lastSeq = 0;
  let pollTimer = null;
  let refreshTimer = null;

  /* ------------------------------------------------------------- routing */
  const PAGES = ["beranda", "monitoring", "aturan", "laporan", "aktivitas"];

  function goto(page, options) {
    if (!PAGES.includes(page)) page = "beranda";
    currentPage = page;
    if (!(options && options.fromHash)) {
      // Keep the address in sync so the view can be linked and Back works.
      if (location.hash !== `#${page}`) location.hash = page;
    }
    $$(".page").forEach((p) => p.classList.toggle("is-active", p.id === `page-${page}`));
    $$(".nav-item").forEach((n) => {
      if (n.dataset.page === page) n.setAttribute("aria-current", "page");
      else n.removeAttribute("aria-current");
    });

    // Agents only stream while the grid is actually open.
    API.setViewing(page === "monitoring", null);

    if (page === "monitoring") P.renderGrid();
    if (page === "aturan") P.renderRules();
    if (page === "laporan") P.renderViolations();
    if (page === "aktivitas") P.renderAudit();
    if (page !== "monitoring") P.stopFrameWatching();
  }

  /* ------------------------------------------------------------ refresh */
  let refreshing = false;

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      await doRefresh();
    } finally {
      refreshing = false;
    }
  }

  async function doRefresh() {
    const res = await API.getOverview();
    if (!res.ok) {
      setOffline(true);
      return;
    }
    setOffline(false);
    P.renderOverview(res);
    updateBadge();
    if (currentPage === "monitoring") P.renderGrid();
  }

  function updateBadge() {
    const badge = $("#nav-badge");
    const flagged = (P.State.counts.dropped || 0);
    badge.hidden = flagged === 0;
    badge.textContent = String(flagged);
  }

  let offlineShown = false;
  function setOffline(off) {
    if (off && !offlineShown) {
      offlineShown = true;
      toast("Dashboard tidak merespons", "Mencoba menyambung lagi…", "error");
    } else if (!off && offlineShown) {
      offlineShown = false;
      toast("Tersambung lagi", "", "ok");
    }
  }

  /* ----------------------------------------------------- live event poll */
  let polling = false;

  async function poll() {
    // Overlapping polls would read the same lastSeq and replay events, so a
    // slow backend must never start a second one.
    if (polling) return;
    polling = true;
    let res;
    try {
      res = await API.pollEvents(lastSeq);
    } finally {
      polling = false;
    }
    if (!res.ok || !res.events) return;
    lastSeq = res.seq;
    let needsRefresh = false;

    for (const ev of res.events) {
      if (ev.topic === "violation.new") {
        const v = ev.data;
        if (v.kind === "agent_tamper") {
          // Always shown, never deduplicated away: it explains a gap in
          // monitoring (killed, service stopped, or the PC lost power).
          toast(`${v.hostname}: agent dihentikan tidak wajar`, v.detail, "error");
        } else if (v.notify) {
          toast(
            `${v.hostname}: ${v.subject}`,
            v.dry_run ? "Dicatat, tidak ditindak" : global.U.violationLabel(v.kind),
            v.dry_run ? "warn" : "error"
          );
        }
        needsRefresh = true;
      } else if (ev.topic === "agent.updated" || ev.topic === "session.updated"
                 || ev.topic === "rules.updated") {
        needsRefresh = true;
      }
    }
    if (needsRefresh) refresh();
  }

  /* --------------------------------------------------------------- theme */
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("ls-theme", theme); } catch (e) { /* private mode */ }
    const use = $("#btn-theme use");
    if (use) use.setAttribute("href", theme === "dark" ? "#i-sun" : "#i-moon");
  }

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("ls-theme"); } catch (e) { /* ignore */ }
    const prefersDark = global.matchMedia
      && global.matchMedia("(prefers-color-scheme: dark)").matches;
    applyTheme(saved || (prefersDark ? "dark" : "light"));
  }

  /* ---------------------------------------------------------- onboarding */
  function maybeOnboard() {
    let seen = null;
    try { seen = localStorage.getItem("ls-onboarded"); } catch (e) { /* ignore */ }
    if (seen) return;
    openDialog({
      title: "Sebelum mulai",
      build: (body) => {
        body.innerHTML = `
          <ul class="points">
            <li><strong>Pasang PC dengan kode 6 digit.</strong>
              Kodenya ada di Aturan. PC menemukan dashboard ini sendiri di jaringan lab;
              alamat IP tidak perlu diisi.</li>
            <li><strong>Awalnya hanya mencatat.</strong>
              Pada pemasangan baru, aplikasi tidak ditutup dan situs tidak diblokir.
              Coba aturannya di satu praktikum, lalu tegakkan dari Beranda.</li>
            <li><strong>Peserta bisa melihatnya.</strong>
              Ikon Lab Sentinel di layar peserta menunjukkan pemantauan aktif atau dijeda.
              Ketikan keyboard tidak direkam.</li>
          </ul>`;
      },
      footer: (foot, close) => {
        const ok = global.U.el('<button class="btn btn--primary" data-autofocus>Mulai</button>');
        ok.addEventListener("click", () => {
          try { localStorage.setItem("ls-onboarded", "1"); } catch (e) { /* ignore */ }
          close();
        });
        foot.appendChild(ok);
      },
    });
  }

  /* ----------------------------------------------------------- listeners */
  function wire() {
    $$(".nav-item").forEach((n) => n.addEventListener("click", () => goto(n.dataset.page)));
    $$("[data-goto]").forEach((n) => n.addEventListener("click", () => goto(n.dataset.goto)));

    $("#btn-theme").addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      applyTheme(next);
    });

    $("#btn-pause-all").addEventListener("click", async (ev) => {
      const total = P.State.agents.length;
      const ok = await confirm({
        title: "Jeda semua PC?",
        message: `Di ${total} PC, aplikasi tidak lagi ditutup dan situs tidak lagi diblokir.`,
        warning: "PC tetap tampil di dashboard dan bisa dilanjutkan kapan saja.",
        confirmLabel: "Jeda semua",
        danger: true,
      });
      if (!ok) return;
      await withBusy(ev.currentTarget, async () => {
        reportToast(await API.pause(null), "Jeda semua");
        refresh();
      });
    });

    $("#btn-resume-all").addEventListener("click", async (ev) => {
      const ok = await confirm({
        title: "Lanjutkan semua PC?",
        message: "Aturan berlaku lagi di semua PC dalam beberapa detik.",
        confirmLabel: "Lanjutkan semua",
      });
      if (!ok) return;
      await withBusy(ev.currentTarget, async () => {
        reportToast(await API.resume(null), "Lanjutkan semua");
        refresh();
      });
    });

    $("#pc-search").addEventListener("input", global.U.debounce((e) => {
      P.State.search = e.target.value;
      P.renderGrid();
    }, 180));

    $$("[data-filter]").forEach((b) => b.addEventListener("click", () => {
      P.State.filter = b.dataset.filter;
      P.syncFilterButtons();
      P.renderGrid();
    }));

    $("#btn-select-mode").addEventListener("click", (e) => {
      P.State.selecting = !P.State.selecting;
      P.State.selected.clear();
      e.currentTarget.textContent = P.State.selecting ? "Selesai memilih" : "Pilih PC";
      P.renderSelectionBar();
      P.renderGrid();
    });

    // Keyboard: 1-5 jump between pages when not typing.
    document.addEventListener("keydown", (e) => {
      if (e.target.matches("input, textarea, select")) return;
      if (e.ctrlKey || e.altKey || e.metaKey) return;
      const index = parseInt(e.key, 10) - 1;
      if (index >= 0 && index < PAGES.length) goto(PAGES[index]);
    });

    global.addEventListener("hashchange", () => {
      const page = location.hash.replace("#", "") || "beranda";
      if (page === currentPage) return;
      // Back/forward while a PC's detail is open: leave the dialog with the page.
      if (!document.getElementById("scrim").hidden) C.closeDialog();
      goto(page, { fromHash: true });
    });

    // Stop asking agents for frames while the window is hidden.
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        API.setViewing(false, null);
        P.stopFrameWatching();
      } else if (currentPage === "monitoring") {
        API.setViewing(true, null);
        P.renderGrid();
      }
    });

    global.addEventListener("beforeunload", () => { API.setViewing(false, null); });
  }

  /* --------------------------------------------------------------- start */
  async function start() {
    initTheme();
    wire();
    if (!API.available) {
      toast("Pratinjau tanpa data", "Halaman ini dibuka tanpa aplikasi dashboard.", "warn");
      return;
    }
    await refresh();
    const startPage = location.hash.replace("#", "");
    if (startPage && startPage !== "beranda") goto(startPage, { fromHash: true });
    maybeOnboard();
    pollTimer = setInterval(poll, 900);
    refreshTimer = setInterval(refresh, 6000);
  }

  global.App = { goto, refresh, currentPage: () => currentPage };
  document.addEventListener("DOMContentLoaded", start);
})(window);
