/* Page renderers.
 *
 * The PC grid only fetches a thumbnail for cards that are actually on screen
 * (IntersectionObserver), so a 40+ PC lab stays smooth and agents off-screen
 * are never asked for frames.
 */
(function (global) {
  "use strict";

  const { $, $$, esc, el, icon, statusChip, statusInfo, ago, clock, datetime, fullDate, stamp,
          violationLabel, duration } = global.U;
  const { toast, reportToast, openDialog, confirm, empty, withBusy } = global.C;

  const State = {
    agents: [],
    counts: {},
    rules: null,
    session: null,
    globalPaused: false,
    filter: "all",
    search: "",
    selecting: false,
    selected: new Set(),
    openAgent: null,
    focusPairing: false,
    reportSession: undefined,  // undefined = not chosen yet; null = all sessions
  };

  /* ===================================================================
     Beranda
     =================================================================== */
  function renderOverview(data) {
    if (data.counts) State.counts = data.counts;
    if (data.agents) State.agents = data.agents;
    if (data.rules) State.rules = data.rules;
    State.session = data.session || null;
    State.globalPaused = !!data.global_paused;

    renderTally();
    renderLabMap();
    renderDryRunBanner();
    renderSessionPanel(data.upcoming || []);
    renderSessionActions();
    renderTopbarSession();
    renderFeed(data.recent_violations || []);
  }

  /* The head count beside the lab map; each figure opens the screens with
     that status filtered. */
  function renderTally() {
    const c = State.counts || {};
    const items = [
      ["online", "aktif"], ["paused", "dijeda"], ["dropped", "terputus"], ["offline", "mati"],
    ];
    $("#stats").innerHTML = items.map(([key, word]) => {
      const n = c[key] || 0;
      return `<button class="tally__item tally__item--${key} ${n ? "" : "is-zero"}" data-stat="${key}"
                      title="Tampilkan PC yang ${word}"><b>${n}</b>${word}</button>`;
    }).join("");
    $$("[data-stat]").forEach((node) => {
      node.addEventListener("click", () => {
        State.filter = node.dataset.stat;
        global.App.goto("monitoring");
        syncFilterButtons();
        renderGrid();
      });
    });
  }

  /* Lab rows from PC names: "LAB-A-01" and "LAB-A-02" sit in row A. Names
     that follow no pattern share one row. */
  function labRows(agents) {
    const rows = new Map();
    agents.slice()
      .sort((x, y) => (x.hostname || x.agent_id).localeCompare(y.hostname || y.agent_id, "id",
                                                              { numeric: true }))
      .forEach((a) => {
        const m = /^(.*?)[-_ ]?(\d+)$/.exec(a.hostname || "");
        const prefix = m ? m[1] : "";
        const key = prefix.split(/[-_ ]/).filter(Boolean).pop() || "";
        if (!rows.has(key)) rows.set(key, []);
        rows.get(key).push(a);
      });
    return rows;
  }

  function renderLabMap() {
    const host = $("#lab-map");
    if (!State.agents.length) {
      host.innerHTML = `<p class="labmap__empty">Belum ada PC terpasang. Kode pemasangan ada di
        <button class="btn btn--sm" id="map-pairing">Aturan</button></p>`;
      $("#map-pairing").addEventListener("click", () => {
        State.focusPairing = true;
        global.App.goto("aturan");
      });
      return;
    }
    const rows = labRows(State.agents);
    const now = Date.now() / 1000;
    host.innerHTML = Array.from(rows.entries()).map(([key, agents]) => `
      <div class="labmap__row">
        <div class="labmap__label">${rows.size > 1 && key ? `Baris ${esc(key)}` : "PC"}</div>
        <div class="labmap__seats">${agents.map((a) => {
          const flagged = a.last_violation_at && (now - a.last_violation_at) < RECENT_VIOLATION_S;
          const s = statusInfo(a.status);
          return `
            <button class="seat seat--${esc(a.status)} ${flagged ? "is-flagged" : ""}"
                    data-seat="${esc(a.agent_id)}"
                    title="${esc(a.hostname)} · ${esc(s.label)}${a.username ? ` · ${esc(a.username)}` : ""}">
              <span class="seat__name">${esc(a.hostname || a.agent_id)}</span>
              <span class="seat__state">${statusChip(a.status)}
                ${a.violation_count ? `<span class="seat__count">${a.violation_count}×</span>` : ""}</span>
            </button>`;
        }).join("")}</div>
      </div>`).join("");
    $$("[data-seat]", host).forEach((node) => {
      node.addEventListener("click", () => openPc(node.dataset.seat));
    });
  }

  function renderDryRunBanner() {
    const host = $("#dry-run-banner");
    if (State.rules && State.rules.mode === "free") {
      host.innerHTML = `
        <div class="banner banner--info">
          <div><strong>Mode Bebas.</strong> Layar dan pelanggaran tetap terpantau, tapi tidak ada
            aplikasi yang ditutup atau situs yang diblokir.</div>
        </div>`;
      return;
    }
    if (!State.rules || !State.rules.dry_run) { host.innerHTML = ""; return; }
    host.innerHTML = `
      <div class="banner">
        <div><strong>Uji coba.</strong> Pelanggaran hanya dicatat: aplikasi tidak ditutup,
          situs tidak diblokir.</div>
        <button class="btn btn--sm" id="btn-leave-dryrun">Tegakkan aturan</button>
      </div>`;
    $("#btn-leave-dryrun").addEventListener("click", async (ev) => {
      const ok = await confirm({
        title: "Tegakkan aturan sekarang?",
        message: "Di semua PC, aplikasi di luar daftar izin akan ditutup dan situs terlarang diblokir.",
        confirmLabel: "Tegakkan",
      });
      if (!ok) return;
      await withBusy(ev.currentTarget, async () => {
        const res = await API.setDryRun(false);
        if (res.ok) { toast("Aturan ditegakkan", `Terkirim ke ${res.reached} PC.`, "ok"); global.App.refresh(); }
        else toast("Gagal", res.error, "error");
      });
    });
  }

  function renderTopbarSession() {
    const host = $("#topbar-session");
    host.classList.toggle("sessionline--live", !!State.session && !State.globalPaused);
    host.classList.toggle("sessionline--paused", !!State.session && State.globalPaused);
    const lamp = '<span class="sessionline__lamp" aria-hidden="true"></span>';
    const sep = '<span class="sessionline__sep" aria-hidden="true">/</span>';
    if (State.session) {
      const started = State.session.started_at;
      host.innerHTML = `
        ${lamp}<span>${State.globalPaused ? "Sesi dijeda" : "Sesi berjalan"}</span>
        <strong class="truncate">${esc(State.session.name)}</strong>
        ${sep}<span>${esc(modeLabel(State.session.mode))}</span>
        ${sep}<span class="num">mulai ${esc(clock(started))} (${esc(duration(Date.now() / 1000 - (started || 0)))})</span>`;
    } else {
      const mode = State.rules ? `${sep}<span>Mode ${esc(modeLabel(State.rules.mode))}</span>` : "";
      host.innerHTML = `${lamp}<span>Tidak ada sesi</span>${mode}`;
    }
  }

  function modeLabel(mode) {
    return { exam: "Ujian", lab: "Praktikum", free: "Bebas" }[mode] || mode;
  }

  function renderSessionActions() {
    const host = $("#session-actions");
    host.innerHTML = State.session
      ? `<button class="btn btn--danger" id="btn-end-session">Akhiri sesi</button>`
      : `<button class="btn btn--primary" id="btn-start-session">Mulai sesi</button>`;
    const start = $("#btn-start-session");
    if (start) start.addEventListener("click", openStartSessionDialog);
    const end = $("#btn-end-session");
    if (end) end.addEventListener("click", async (ev) => {
      const ok = await confirm({
        title: "Akhiri sesi?",
        message: `"${State.session.name}" ditutup dan pemantauan dijeda di semua PC.`,
        confirmLabel: "Akhiri sesi",
        danger: true,
      });
      if (!ok) return;
      await withBusy(ev.currentTarget, async () => {
        const res = await API.endSession();
        if (res.ok) { toast("Sesi diakhiri", "Pemantauan dijeda di semua PC.", "ok"); global.App.refresh(); }
        else toast("Gagal", res.error, "error");
      });
    });
  }

  /* What each mode allows; the Aturan page reads the same words from
     domain/session_presets.py, so keep the two in step. */
  const MODE_SPEC = {
    exam: { apps: "Hanya Chrome dan Edge", sites: "Semua kategori dan situs tambahan diblokir" },
    lab: { apps: "Browser dan aplikasi yang diizinkan", sites: "Kategori pilihan dan situs tambahan diblokir" },
    free: { apps: "Tidak ada yang ditutup", sites: "Tidak ada yang diblokir" },
  };

  function modeOption(key, label, spec, pressed, current) {
    return `
      <button class="mode" data-mode="${esc(key)}" aria-pressed="${pressed}">
        <span class="mode__top"><span class="mode__radio" aria-hidden="true"></span>
          <span class="mode__name">${esc(label)}</span>
          ${current ? '<span class="mode__now">dipakai</span>' : ""}</span>
        <dl class="mode__spec">
          <dt>Aplikasi</dt><dd>${esc(spec.apps)}</dd>
          <dt>Situs</dt><dd>${esc(spec.sites)}</dd>
        </dl>
      </button>`;
  }

  function openStartSessionDialog() {
    openDialog({
      title: "Mulai sesi",
      subtitle: "Pelanggaran selama sesi masuk ke laporan sesi ini.",
      build: (body) => {
        const current = (State.rules && State.rules.mode) || "lab";
        body.innerHTML = `
          <label style="display:block;margin-bottom:var(--s-4)">
            <span class="label">Nama sesi</span>
            <input class="input" id="sess-name" data-autofocus
                   placeholder="UTS Algoritma, Kelas A">
          </label>
          <span class="label" style="margin-bottom:var(--s-2)">Mode</span>
          <div class="mode-row" id="sess-modes">
            ${["exam", "lab", "free"].map((m) =>
              modeOption(m, modeLabel(m), MODE_SPEC[m], m === current, false)).join("")}
          </div>`;
        $$("[data-mode]", body).forEach((btn) => {
          btn.addEventListener("click", () => {
            $$("[data-mode]", body).forEach((b) => b.setAttribute("aria-pressed", "false"));
            btn.setAttribute("aria-pressed", "true");
          });
        });
      },
      footer: (foot, close) => {
        const cancel = el('<button class="btn">Batal</button>');
        const go = el('<button class="btn btn--primary">Mulai sesi</button>');
        cancel.addEventListener("click", close);
        go.addEventListener("click", async () => {
          const name = $("#sess-name").value.trim();
          if (!name) { toast("Nama sesi belum diisi", "Nama ini dipakai sebagai judul laporan.", "warn"); return; }
          const mode = ($('#sess-modes [aria-pressed="true"]') || {}).dataset.mode || "lab";
          await withBusy(go, async () => {
            const res = await API.startSession(name, mode);
            if (res.ok) { close(); toast("Sesi dimulai", `${name}, mode ${modeLabel(mode)}.`, "ok"); global.App.refresh(); }
            else toast("Sesi gagal dimulai", res.error, "error");
          });
        });
        foot.append(cancel, go);
      },
    });
  }

  function renderSessionPanel(upcoming) {
    const host = $("#session-panel");
    const parts = [];
    if (State.session) {
      const s = State.session;
      parts.push(`
        <div class="session">
          <div class="session__name">${esc(s.name)}</div>
          <dl class="session__facts">
            <dt>Mode</dt><dd>${esc(modeLabel(s.mode))}</dd>
            <dt>Mulai</dt><dd class="num">${esc(clock(s.started_at))}</dd>
            <dt>Berjalan</dt><dd>${esc(duration(Date.now() / 1000 - (s.started_at || 0)))}</dd>
            <dt>Pemantauan</dt><dd>${State.globalPaused ? "Dijeda" : "Aktif"}</dd>
          </dl>
        </div>`);
    } else {
      parts.push(`
        <div class="empty">
          <div class="empty__title">Tidak ada sesi berjalan.</div>
          <p class="empty__text">Pelanggaran tetap dicatat, tapi tidak masuk ke laporan sesi mana pun.
            Mulai sesi saat praktikum atau ujian dimulai.</p>
        </div>`);
    }
    if (upcoming.length) {
      parts.push(`
        <div class="session__sched">
          <div class="subhead" style="margin-top:0">Terjadwal</div>
          ${upcoming.map((s) => `
            <div class="session__sched-item">
              <span class="truncate">${esc(s.name)}</span>
              <span class="muted">${esc(fullDate(s.scheduled_start))}</span>
            </div>`).join("")}
        </div>`);
    }
    host.innerHTML = parts.join("");
  }

  function renderFeed(violations) {
    const host = $("#recent-feed");
    host.innerHTML = "";
    if (!violations.length) {
      host.appendChild(empty({
        title: "Belum ada pelanggaran.",
        text: "Setiap pelanggaran muncul di sini dengan tangkapan layarnya.",
      }));
      return;
    }
    violations.forEach((v) => host.appendChild(feedItem(v)));
  }

  function feedItem(v) {
    const node = el(`
      <div class="feed__item ${v.dry_run ? "is-dry" : ""}" role="button" tabindex="0"
           title="${esc(fullDate(v.occurred_at))}">
        <span class="feed__time">${esc(stamp(v.occurred_at))}</span>
        <span class="feed__pc">${esc(v.hostname || v.agent_id)}</span>
        <span class="feed__body">
          <span class="feed__title">${esc(v.subject)}</span>
          <span class="feed__sub">
            <span class="tag tag--plain">${esc(violationLabel(v.kind))}</span>
            ${v.dry_run ? '<span class="tag tag--muted">dicatat saja</span>' : ""}
          </span>
        </span>
      </div>`);
    const open = () => openViolation(v);
    node.addEventListener("click", open);
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
    return node;
  }

  async function openViolation(v) {
    openDialog({
      title: v.subject,
      subtitle: `${v.hostname || v.agent_id} · ${fullDate(v.occurred_at)}`,
      wide: true,
      build: (body) => {
        const notes = [violationLabel(v.kind)];
        if (v.detected_from_title) notes.push("terbaca dari judul jendela browser, jadi bisa keliru");
        if (v.dry_run) notes.push("hanya dicatat, tidak ditindak");
        body.innerHTML = `
          <p style="margin-bottom:var(--s-3)">${esc(notes.join("; "))}.</p>
          ${v.detail ? `<p class="muted" style="margin-bottom:var(--s-4)">${esc(v.detail)}</p>` : ""}
          <div id="shot-host"></div>`;
        const shotHost = $("#shot-host", body);
        if (!v.has_screenshot) {
          shotHost.appendChild(empty({
            title: "Tidak ada tangkapan layar untuk pelanggaran ini.",
          }));
          return;
        }
        shotHost.innerHTML = '<div class="skeleton" style="aspect-ratio:16/9;width:100%"></div>';
        API.getScreenshot(v.id).then((res) => {
          if (res.ok && res.image) {
            shotHost.innerHTML =
              `<div class="detail__screen"><img src="data:image/jpeg;base64,${res.image}"
                    alt="Tangkapan layar ${esc(v.hostname || "")} saat pelanggaran"></div>`;
          } else {
            shotHost.innerHTML = "";
            shotHost.appendChild(empty({
              title: "Tangkapan layar tidak bisa dibuka.",
              text: res.error || "Berkasnya mungkin sudah terhapus karena melewati masa simpan.",
            }));
          }
        });
      },
    });
  }

  /* ===================================================================
     Monitoring grid
     =================================================================== */
  let frameObserver = null;
  // Live grid: cards on screen (id -> card) and the frame number each shows.
  const liveCards = new Map();
  const shownSeq = new Map();
  let framePump = null;
  let frameRequestInFlight = false;
  const FRAME_POLL_MS = 200;
  // Set while a PC's detail is open: brings its status and buttons up to date.
  let syncOpenDetail = null;

  function visibleAgents() {
    const q = State.search.trim().toLowerCase();
    return State.agents.filter((a) => {
      if (State.filter !== "all" && a.status !== State.filter) return false;
      if (q && !(a.hostname || "").toLowerCase().includes(q)
            && !(a.agent_id || "").toLowerCase().includes(q)) return false;
      return true;
    });
  }

  function renderGrid() {
    const host = $("#pc-grid");
    const list = visibleAgents();

    if (!State.agents.length || !list.length) {
      stopFrameWatching();
      host.innerHTML = "";
    }
    if (!State.agents.length) {
      host.appendChild(empty({
        title: "Belum ada PC terpasang.",
        text: "Pasang agent di PC peserta, lalu masukkan kode pemasangan 6 digit. PC menemukan dashboard ini sendiri di jaringan lab; alamat IP tidak perlu diisi.",
        actionLabel: "Lihat kode pemasangan",
        onAction: () => { State.focusPairing = true; global.App.goto("aturan"); },
      }));
      return;
    }
    if (!list.length) {
      host.appendChild(empty({
        title: "Tidak ada PC yang cocok.",
        actionLabel: "Tampilkan semua PC",
        onAction: () => { State.filter = "all"; State.search = ""; $("#pc-search").value = ""; syncFilterButtons(); renderGrid(); },
      }));
      return;
    }

    // Refresh in place: every card's details are rebuilt (status, tags, name),
    // but a live screen already on show is carried over, so the grid never
    // blanks while heartbeats keep it up to date.
    const previous = new Map($$("#pc-grid .pc").map((n) => [n.dataset.agent, n]));
    const cards = list.map((a) => {
      const card = pcCard(a);
      const old = previous.get(a.agent_id);
      const img = old && hasLiveScreen(a) ? old.querySelector("[data-screen] img") : null;
      if (img) adoptScreen(card, img);
      else shownSeq.delete(a.agent_id);  // nothing on show: fetch at once
      return card;
    });
    host.replaceChildren(...cards);
    const shown = new Set(list.map((a) => a.agent_id));
    [...shownSeq.keys()].forEach((id) => { if (!shown.has(id)) shownSeq.delete(id); });
    watchCards(cards);
    if (syncOpenDetail) syncOpenDetail();
    $("#monitoring-sub").textContent = list.length === State.agents.length
      ? `${list.length} PC`
      : `${list.length} dari ${State.agents.length} PC`;
  }

  /* A disconnected PC has no screen to show (the dashboard drops its last
     frame), so its old picture must not be carried over. */
  function hasLiveScreen(agent) {
    return agent.status === "online" || agent.status === "paused";
  }

  function adoptScreen(card, img) {
    const screen = card.querySelector("[data-screen]");
    const placeholder = screen.querySelector(".pc__placeholder");
    if (placeholder) placeholder.remove();
    screen.insertBefore(img, screen.firstChild);
  }

  // A PC is highlighted only while it is misbehaving *now*. Highlighting every
  // PC that ever had a violation would turn the whole grid red by mid-session.
  const RECENT_VIOLATION_S = 120;

  function pcCard(a) {
    const recent = a.last_violation_at
      && (Date.now() / 1000 - a.last_violation_at) < RECENT_VIOLATION_S;
    const flagged = !!recent;
    const node = el(`
      <div class="pc ${flagged ? "is-flagged" : ""} ${State.selected.has(a.agent_id) ? "is-selected" : ""}"
           role="button" tabindex="0" data-agent="${esc(a.agent_id)}"
           aria-label="${esc(a.hostname)} — ${esc(statusInfo(a.status).label)}">
        <div class="pc__screen" data-screen>
          <div class="pc__placeholder">
            ${icon(a.status === "online" ? "monitor" : "eye-off")}
            <span>${esc(a.status === "online" ? "Memuat layar…" : statusInfo(a.status).label)}</span>
          </div>
          ${a.desktop_access === false
            ? '<div class="pc__overlay"><span class="pc__tag">layar tidak terjangkau</span></div>'
            : a.indicator_visible === false
              ? '<div class="pc__overlay"><span class="pc__tag">indikator disembunyikan</span></div>' : ""}
          ${a.input_frozen ? '<span class="pc__frozen">Input dibekukan</span>' : ""}
        </div>
        <div class="pc__meta">
          <div style="min-width:0">
            <div class="pc__name truncate">${esc(a.hostname || a.agent_id)}</div>
            <div class="pc__sub truncate">${esc(a.username || "—")}</div>
          </div>
          <div class="pc__right">
            ${a.violation_count > 0
              ? `<span class="pc__count ${recent ? "" : "pc__count--past"}"
                       title="${a.violation_count} pelanggaran${recent ? ", terakhir barusan" : ""}">
                   ${icon("alert", "")}${a.violation_count}</span>`
              : ""}
            ${statusChip(a.status)}
          </div>
        </div>
      </div>`);

    if (State.selecting) {
      const box = el(`<input class="pc__select" type="checkbox" aria-label="Pilih ${esc(a.hostname)}"
                       ${State.selected.has(a.agent_id) ? "checked" : ""}>`);
      box.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (box.checked) State.selected.add(a.agent_id);
        else State.selected.delete(a.agent_id);
        node.classList.toggle("is-selected", box.checked);
        renderSelectionBar();
      });
      node.querySelector("[data-screen]").appendChild(box);
    }

    const activate = () => {
      if (State.selecting) {
        const box = node.querySelector(".pc__select");
        if (box) box.click();
        return;
      }
      openPc(a.agent_id);
    };
    node.addEventListener("click", activate);
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); }
    });
    return node;
  }

  /* Live grid: one request fetches every new frame for the cards on screen
     (cards scrolled out of view are not asked for). */
  function onCardsVisible(entries) {
    entries.forEach((entry) => {
      const id = entry.target.dataset.agent;
      if (entry.isIntersecting) liveCards.set(id, entry.target);
      else liveCards.delete(id);
    });
  }

  function watchCards(cards) {
    if (frameObserver) frameObserver.disconnect();
    else frameObserver = new IntersectionObserver(onCardsVisible, { rootMargin: "120px" });
    // Cards that replaced visible ones stay live straight away; the observer
    // corrects the set as soon as it has measured the new cards.
    const wasLive = new Set(liveCards.keys());
    liveCards.clear();
    cards.forEach((card) => {
      if (wasLive.has(card.dataset.agent)) liveCards.set(card.dataset.agent, card);
      frameObserver.observe(card);
    });
    if (!framePump) pumpFrames();
  }

  function stopFrameWatching() {
    if (frameObserver) { frameObserver.disconnect(); frameObserver = null; }
    if (framePump) { clearTimeout(framePump); framePump = null; }
    liveCards.clear();
    shownSeq.clear();  // the next grid starts empty: paint every card at once
  }

  async function pumpFrames() {
    framePump = setTimeout(pumpFrames, FRAME_POLL_MS);  // keep the beat
    if (!liveCards.size || frameRequestInFlight) return;
    frameRequestInFlight = true;
    try {
      const seen = {};
      liveCards.forEach((_, id) => { seen[id] = shownSeq.get(id) || 0; });
      const res = await API.getFrames(seen);
      if (!res.ok) return;
      res.frames.forEach((f) => {
        const card = liveCards.get(f.agent_id);
        if (!card) return;
        paintScreen(card.querySelector("[data-screen]"), f.jpeg_b64, "");
        shownSeq.set(f.agent_id, f.seq);
      });
    } finally {
      frameRequestInFlight = false;
    }
  }

  function paintScreen(screen, jpegB64, alt) {
    if (!screen) return;
    let img = screen.querySelector("img");
    if (!img) {
      img = document.createElement("img");
      img.alt = alt;
      const ph = screen.querySelector(".pc__placeholder");
      if (ph) ph.remove();
      screen.insertBefore(img, screen.firstChild);
    }
    img.src = `data:image/jpeg;base64,${jpegB64}`;
  }

  function syncFilterButtons() {
    $$("[data-filter]").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.filter === State.filter)));
  }

  function renderSelectionBar() {
    const host = $("#selection-bar");
    host.hidden = !State.selecting;
    if (!State.selecting) return;
    const n = State.selected.size;
    host.innerHTML = `
      <div style="display:flex;align-items:center;gap:var(--s-2)">
        <span class="num" style="font-size:var(--text-sm);color:var(--text-secondary)">
          ${n} dipilih</span>
        <button class="btn btn--sm" id="sel-pause" ${n ? "" : "disabled"}>Jeda</button>
        <button class="btn btn--sm" id="sel-resume" ${n ? "" : "disabled"}>Lanjutkan</button>
        <button class="btn btn--sm" id="sel-lock" ${n ? "" : "disabled"}>Kunci layar</button>
        <button class="btn btn--sm" id="sel-msg" ${n ? "" : "disabled"}>Kirim pesan</button>
        <button class="btn btn--sm btn--ghost" id="sel-cancel">Selesai</button>
      </div>`;
    const ids = () => Array.from(State.selected);
    $("#sel-pause").addEventListener("click", (e) => massAction(e.currentTarget, "Jeda", () => API.pause(ids()), ids().length));
    $("#sel-resume").addEventListener("click", (e) => massAction(e.currentTarget, "Lanjutkan", () => API.resume(ids()), ids().length));
    $("#sel-lock").addEventListener("click", (e) => massAction(e.currentTarget, "Kunci layar", () => API.lockScreens(ids()), ids().length, true));
    $("#sel-msg").addEventListener("click", () => openBroadcastDialog(ids()));
    $("#sel-cancel").addEventListener("click", () => {
      State.selecting = false; State.selected.clear();
      $("#btn-select-mode").textContent = "Pilih PC";
      renderSelectionBar(); renderGrid();
    });
  }

  async function massAction(button, verb, run, count, danger) {
    const ok = await confirm({
      title: `${verb} di ${count} PC?`,
      message: `Perintah dikirim ke ${count} PC sekaligus.`,
      confirmLabel: verb,
      danger: !!danger,
    });
    if (!ok) return;
    await withBusy(button, async () => {
      reportToast(await run(), verb);
      global.App.refresh();
    });
  }

  function openBroadcastDialog(ids) {
    openDialog({
      title: "Kirim pesan",
      subtitle: ids && ids.length ? `Ke ${ids.length} PC yang dipilih` : "Ke semua PC",
      build: (body) => {
        body.innerHTML = `
          <textarea class="input" id="bc-text" data-autofocus maxlength="500"
                    placeholder="Waktu tinggal 10 menit. Simpan pekerjaan Anda."></textarea>
          <p class="hint">Muncul sebagai notifikasi kecil di layar peserta; jendela yang sedang
            dipakai tidak berpindah.</p>`;
      },
      footer: (foot, close) => {
        const cancel = el('<button class="btn">Batal</button>');
        const send = el('<button class="btn btn--primary">Kirim</button>');
        cancel.addEventListener("click", close);
        send.addEventListener("click", async () => {
          const text = $("#bc-text").value.trim();
          if (!text) { toast("Pesan masih kosong", "", "warn"); return; }
          await withBusy(send, async () => {
            reportToast(await API.broadcast(text, ids && ids.length ? ids : null), "Kirim pesan");
            close();
          });
        });
        foot.append(cancel, send);
      },
    });
  }

  /* ---- single PC ---- */
  // Stops the PC detail on show (its frame timer and close watcher), if any.
  let closeOpenDetail = null;

  function openPc(agentId) {
    const found = State.agents.find((a) => a.agent_id === agentId);
    if (!found) return null;
    // A confirmation replaces this view and reopens it afterwards: make sure
    // the previous one's timer and watcher never outlive it.
    if (closeOpenDetail) closeOpenDetail();
    const agent = { ...found };
    State.openAgent = agentId;
    API.setViewing(true, agentId);

    let detailTimer = null;
    let paintFreeze = () => {};
    // Confirmations take over the dialog; come back to this PC afterwards.
    const confirmHere = async (options) => {
      const ok = await confirm(options);
      openPc(agentId);
      return ok;
    };

    const dialog = openDialog({
      title: agent.hostname || agentId,
      subtitle: [agent.username, agent.remote_addr].filter(Boolean).join(" · ") || " ",
      wide: true,
      build: (body) => {
        body.innerHTML = `
          <div class="detail__screen" id="detail-screen">
            <div class="pc__placeholder">${icon("monitor")}<span>Memuat layar…</span></div>
          </div>
          <div class="detail__facts" id="detail-facts"></div>`;
        paintDetail(agent);
        // Live: ask often, but only new frames come back (the agent streams
        // this PC at a higher rate while it is open).
        let seq = 0;
        let busy = false;
        detailTimer = setInterval(async () => {
          if (busy) return;
          busy = true;
          try {
            const res = await API.getFrames({ [agentId]: seq });
            if (res.ok && res.frames.length) {
              const f = res.frames[0];
              paintScreen($("#detail-screen"), f.jpeg_b64, "Layar peserta");
              seq = f.seq;
            }
          } finally {
            busy = false;
          }
        }, 150);
      },
      footer: (foot, close) => {
        const pause = el('<button class="btn">Jeda</button>');
        const resume = el('<button class="btn">Lanjutkan</button>');
        const lock = el('<button class="btn">Kunci layar</button>');
        const freeze = el('<button class="btn"></button>');
        paintFreeze = () => {
          const frozen = !!agent.input_frozen;
          freeze.textContent = frozen ? "Cairkan input" : "Bekukan input";
          freeze.classList.toggle("btn--warn", frozen);
          // No desktop worker means no way to freeze this PC right now.
          freeze.disabled = agent.desktop_access === false;
          freeze.title = freeze.disabled ? "Layar PC ini sedang tidak terjangkau" : "Keyboard dan mouse peserta";
        };
        paintFreeze();
        const done = el('<button class="btn">Tutup</button>');
        pause.addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
          reportToast(await API.pause([agentId]), "Jeda"); global.App.refresh();
        }));
        resume.addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
          reportToast(await API.resume([agentId]), "Lanjutkan"); global.App.refresh();
        }));
        lock.addEventListener("click", async () => {
          const ok = await confirmHere({
            title: "Kunci layar PC ini?",
            message: "Peserta harus mengetik kata sandi Windows untuk masuk lagi.",
            confirmLabel: "Kunci layar", danger: true,
          });
          if (ok) reportToast(await API.lockScreens([agentId]), "Kunci layar");
        });
        freeze.addEventListener("click", async (e) => {
          const turningOn = !agent.input_frozen;
          const send = async () => {
            const res = await API.freezeInput(turningOn, [agentId]);
            reportToast(res, turningOn ? "Bekukan input" : "Cairkan input");
            if (res.ok) setFrozen(agentId, turningOn);
            global.App.refresh();
          };
          if (!turningOn) {  // releasing needs no confirmation
            await withBusy(e.currentTarget, send);
            return;
          }
          const ok = await confirmHere({
            title: "Bekukan keyboard dan mouse?",
            message: "Keyboard dan mouse peserta mati sampai Anda cairkan. Layarnya tetap "
              + "seperti sekarang, tanpa pesan apa pun, jadi yang sedang dibuka bisa Anda lihat.",
            confirmLabel: "Bekukan", danger: true,
          });
          if (ok) await send();
        });
        done.addEventListener("click", close);
        const actions = el('<div class="dialog__foot-start"></div>');
        actions.append(pause, resume, lock, freeze);
        foot.append(actions, done);
      },
    });

    // Keep the open view in step with heartbeats (status, freeze, facts).
    syncOpenDetail = () => {
      const latest = State.agents.find((a) => a.agent_id === agentId);
      if (latest) Object.assign(agent, latest);
      paintDetail(agent);
      paintFreeze();
    };

    // Stop the hi-res stream as soon as the admin closes the view.
    const scrim = $("#scrim");
    const observer = new MutationObserver(() => { if (scrim.hidden) stop(); });
    const stop = () => {
      observer.disconnect();
      clearInterval(detailTimer);
      if (closeOpenDetail !== stop) return;  // already replaced by a newer view
      closeOpenDetail = null;
      syncOpenDetail = null;
      State.openAgent = null;
      API.setViewing(global.App.currentPage() === "monitoring", null);
    };
    closeOpenDetail = stop;
    observer.observe(scrim, { attributes: true, attributeFilter: ["hidden"] });
    return dialog;
  }

  /* Record a freeze change locally until the next heartbeat confirms it. */
  function setFrozen(agentId, frozen) {
    const a = State.agents.find((x) => x.agent_id === agentId);
    if (a) a.input_frozen = frozen;
    if (syncOpenDetail) syncOpenDetail();
  }

  function paintDetail(agent) {
    const unreachable = agent.desktop_access === false;
    const facts = [
      ["Status", statusInfo(agent.status).label],
      ["Pelanggaran", String(agent.violation_count || 0), agent.violation_count > 0],
      ["Jendela aktif", agent.active_window_title || "—", false, true],
      ["Keyboard & mouse", agent.input_frozen ? "Dibekukan" : "Normal", !!agent.input_frozen],
      ["Akses layar", unreachable ? "Tidak terjangkau" : "Normal", unreachable,
        false, unreachable ? "Tangkapan layar, judul jendela, dan penutupan aplikasi tidak berjalan" : ""],
      ["Ikon di layar peserta", agent.indicator_visible === false ? "Disembunyikan" : "Terlihat",
        agent.indicator_visible === false],
      ["Kabar terakhir", ago(agent.last_heartbeat)],
      ["Versi aturan", String(agent.rule_revision)],
      ["CPU / RAM agent", `${(agent.cpu_percent || 0).toFixed(1)}% · ${Math.round(agent.mem_mb || 0)} MB`],
    ];
    const host = $("#detail-facts");
    if (host) {
      host.innerHTML = facts.map(([k, v, alert, wide, hint]) => `
        <div class="fact ${wide ? "fact--wide" : ""}"><div class="fact__label">${esc(k)}</div>
        <div class="fact__value truncate ${alert ? "is-alert" : ""}" title="${esc(hint || v)}">${esc(v)}</div></div>`).join("");
    }
  }

  /* ===================================================================
     Aturan / Laporan / Aktivitas
     =================================================================== */
  async function renderRules() {
    const host = $("#rules-content");
    host.innerHTML = '<div class="skeleton" style="height:160px"></div>';
    const res = await API.getRules();
    if (!res.ok) {
      host.innerHTML = "";
      host.appendChild(empty({ title: "Aturan tidak bisa dimuat.", text: res.error }));
      return;
    }
    const rules = res.rules;
    State.rules = rules;

    host.innerHTML = `
      <section class="block" aria-labelledby="t-mode">
        <div class="block__head"><h2 class="block__title" id="t-mode">Mode</h2>
          <span class="block__aside num">Versi aturan ${esc(rules.revision)}</span></div>
        <div class="block__body"><div class="mode-row" id="mode-row"></div></div>
      </section>

      <div class="rules-cols">
        <section class="block" aria-labelledby="t-apps">
          <div class="block__head"><h2 class="block__title" id="t-apps">Aplikasi yang diizinkan</h2></div>
          <div class="block__body">
            ${appsNote(rules)}
            <div class="chip-input" id="wl-chips"></div>
            <div class="add-row">
              <input class="input" id="wl-new" placeholder="code.exe" aria-label="Nama berkas aplikasi">
              <button class="btn" id="wl-add">Tambah</button>
            </div>
            <p class="hint">Nama berkasnya ada di Task Manager, tab Details.</p>
          </div>
        </section>

        <section class="block" aria-labelledby="t-sites">
          <div class="block__head"><h2 class="block__title" id="t-sites">Situs yang diblokir</h2>
            <span class="block__aside num" id="site-count">${siteCountLabel(rules)}</span></div>
          <div class="block__body">
            ${sitesNote(rules)}
            <div class="check-list" id="cat-list"></div>
            <div class="subhead">Situs tambahan</div>
            <p class="block__note" style="margin-bottom:var(--s-2)">Tempel alamatnya. Subdomain ikut
              diblokir; berlaku di Praktikum dan Ujian.</p>
            <div class="chip-input" id="site-chips"></div>
            <div class="add-row">
              <input class="input" id="site-new" placeholder="tiktok.com" aria-label="Alamat situs">
              <button class="btn" id="site-add">Tambah</button>
            </div>
          </div>
        </section>
      </div>

      <section class="block" aria-labelledby="t-behaviour">
        <div class="block__head"><h2 class="block__title" id="t-behaviour">Penindakan</h2></div>
        <div class="block__body">
          <label class="switch setting">
            <input type="checkbox" id="opt-dryrun" ${rules.dry_run ? "checked" : ""}>
            <span>
              <span class="label" style="margin:0">Uji coba: hanya catat, jangan tindak</span>
              <span class="hint" style="display:block;margin:0">Aplikasi tidak ditutup dan situs tidak
                diblokir; pelanggaran tetap masuk laporan.
                ${rules.mode === "free" ? "Mode Bebas memang tidak menindak apa pun." : ""}</span>
            </span>
          </label>
          <div class="setting">
            <label class="label" for="opt-grace">Waktu peringatan sebelum aplikasi ditutup</label>
            <div class="inline-row">
              <input class="input num" type="number" id="opt-grace" min="0" max="60"
                     value="${rules.close_grace_seconds}" style="width:84px">
              <span class="muted">detik</span>
              <button class="btn btn--sm" id="opt-grace-save">Simpan</button>
            </div>
            <p class="hint">Peserta sempat menyimpan pekerjaannya dulu.</p>
          </div>
        </div>
      </section>

      <section class="block" id="pairing-card" aria-labelledby="t-pairing"></section>`;
    renderPairing();

    // modes
    $("#mode-row").innerHTML = res.modes.map((m) =>
      modeOption(m.key, m.label, { apps: m.apps, sites: m.sites },
                 rules.mode === m.key, rules.mode === m.key)).join("");
    $$("#mode-row [data-mode]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (btn.getAttribute("aria-pressed") === "true") return;
        const label = btn.querySelector(".mode__name").textContent;
        const ok = await confirm({
          title: `Pakai mode ${label}?`,
          message: "Aturan baru langsung terkirim ke semua PC yang terhubung.",
          confirmLabel: `Pakai ${label}`,
        });
        if (!ok) return;
        await withBusy(btn, async () => {
          const r = await API.setMode(btn.dataset.mode);
          if (r.ok) { toast(`Mode ${label} dipakai`, `Terkirim ke ${r.reached} PC.`, "ok"); renderRules(); global.App.refresh(); }
          else toast("Gagal", r.error, "error");
        });
      });
    });

    // whitelist
    const locked = ["chrome.exe", "msedge.exe", "explorer.exe"];
    function paintChips() {
      $("#wl-chips").innerHTML =
        locked.map((n) => `<span class="chip chip--locked">${esc(n)}</span>`).join("") +
        rules.app_whitelist.map((n) => `
          <span class="chip">${esc(n)}
            <button data-remove="${esc(n)}" aria-label="Hapus ${esc(n)}">${icon("x", "")}</button>
          </span>`).join("");
      $$("#wl-chips [data-remove]").forEach((b) => b.addEventListener("click", async () => {
        const next = rules.app_whitelist.filter((x) => x !== b.dataset.remove);
        const r = await API.setWhitelist(next);
        if (r.ok) { rules.app_whitelist = r.rules.app_whitelist; paintChips(); toast("Aplikasi dihapus", b.dataset.remove, "ok"); }
        else toast("Gagal", r.error, "error");
      }));
    }
    paintChips();
    const addApp = async () => {
      const name = $("#wl-new").value.trim().toLowerCase();
      if (!name) return;
      if (!name.endsWith(".exe")) { toast("Tulis nama berkas lengkap", "Misalnya code.exe", "warn"); return; }
      const r = await API.setWhitelist(rules.app_whitelist.concat([name]));
      if (r.ok) { rules.app_whitelist = r.rules.app_whitelist; $("#wl-new").value = ""; paintChips(); toast("Aplikasi ditambahkan", name, "ok"); }
      else toast("Gagal", r.error, "error");
    };
    $("#wl-add").addEventListener("click", addApp);
    $("#wl-new").addEventListener("keydown", (e) => { if (e.key === "Enter") addApp(); });

    // site categories
    const active = new Set(rules.site_categories.map(normalizeCat));
    $("#cat-list").innerHTML = res.categories.map((c) => `
      <label class="check">
        <input type="checkbox" data-cat="${esc(c.key)}" ${active.has(c.key) ? "checked" : ""}>
        <span class="check__main">
          <span class="check__name">${esc(c.label)}</span>
          <span class="check__desc" style="display:block">${esc(c.description)}</span>
        </span>
        <span class="check__count">${c.count} situs</span>
      </label>`).join("");
    $$("#cat-list [data-cat]").forEach((box) => box.addEventListener("change", async () => {
      const keys = $$("#cat-list [data-cat]").filter((b) => b.checked).map((b) => b.dataset.cat);
      const r = await API.setSiteCategories(keys);
      if (r.ok) { paintSiteCount(r.rules); toast("Daftar situs disimpan", siteCountText(r.rules), "ok"); }
      else { toast("Gagal", r.error, "error"); box.checked = !box.checked; }
    }));

    // sites added by hand
    function paintSiteCount(next) {
      rules.site_count = next.site_count;
      $("#site-count").textContent = siteCountLabel(next);
    }
    function paintSites() {
      $("#site-chips").innerHTML = rules.custom_sites.length
        ? rules.custom_sites.map((p) => `
            <span class="chip">${esc(p)}
              <button data-remove-site="${esc(p)}" aria-label="Hapus ${esc(p)}">${icon("x", "")}</button>
            </span>`).join("")
        : '<span class="chip-input__none">Belum ada</span>';
      $$("#site-chips [data-remove-site]").forEach((b) => b.addEventListener("click", async () => {
        const next = rules.custom_sites.filter((x) => x !== b.dataset.removeSite);
        const r = await API.setCustomSites(next);
        if (r.ok) { rules.custom_sites = r.rules.custom_sites; paintSites(); paintSiteCount(r.rules); toast("Situs dihapus", b.dataset.removeSite, "ok"); }
        else toast("Gagal", r.error, "error");
      }));
    }
    paintSites();
    const addSite = async () => {
      const text = $("#site-new").value.trim();
      if (!text) return;
      const r = await API.setCustomSites(rules.custom_sites.concat([text]));
      if (r.ok) {
        const added = r.rules.custom_sites.filter((x) => !rules.custom_sites.includes(x));
        rules.custom_sites = r.rules.custom_sites; $("#site-new").value = ""; paintSites(); paintSiteCount(r.rules);
        toast(added.length ? "Situs ditambahkan" : "Sudah ada di daftar",
              added.length ? added.join(", ") : "", added.length ? "ok" : "info");
      } else toast("Alamat tidak valid", r.error, "warn");
    };
    $("#site-add").addEventListener("click", addSite);
    $("#site-new").addEventListener("keydown", (e) => { if (e.key === "Enter") addSite(); });

    // behaviour
    $("#opt-dryrun").addEventListener("change", async (e) => {
      const on = e.target.checked;
      const r = await API.setDryRun(on);
      if (r.ok) { toast(on ? "Uji coba: hanya mencatat" : "Aturan ditegakkan", "", "ok"); global.App.refresh(); }
      else { toast("Gagal", r.error, "error"); e.target.checked = !on; }
    });
    $("#opt-grace-save").addEventListener("click", async (e) => {
      await withBusy(e.currentTarget, async () => {
        const r = await API.setCloseGrace(parseInt($("#opt-grace").value, 10));
        if (r.ok) toast("Tersimpan", "", "ok"); else toast("Gagal", r.error, "error");
      });
    });
  }

  /* What the app and site lists do in the current mode (Aturan page). */
  function appsNote(rules) {
    if (rules.mode === "exam") {
      return `<div class="banner banner--info"><div><strong>Ujian: hanya browser.</strong>
        Daftar ini tidak berlaku sekarang dan disimpan untuk Praktikum.</div></div>`;
    }
    const action = rules.acts ? "ditutup" : "hanya dicatat";
    return `<p class="block__note" style="margin-bottom:var(--s-3)">Aplikasi lain ${action}.
      Chrome, Edge, dan Windows Explorer selalu boleh.</p>`;
  }

  function sitesNote(rules) {
    if (rules.mode === "exam") {
      return `<div class="banner banner--info"><div><strong>Ujian: semua kategori diblokir.</strong>
        Centang di bawah berlaku untuk Praktikum.</div></div>`;
    }
    if (rules.mode === "free") {
      return `<div class="banner banner--info"><div><strong>Bebas: tidak ada yang diblokir.</strong>
        Daftar ini dipakai saat Praktikum dan Ujian.</div></div>`;
    }
    return "";
  }

  function siteCountLabel(rules) {
    return rules.mode === "free" ? "tidak berlaku di Bebas" : `${rules.site_count} situs diblokir`;
  }

  function siteCountText(rules) {
    return rules.mode === "free"
      ? "Berlaku saat Praktikum dan Ujian."
      : `${rules.site_count} situs diblokir.`;
  }

  /* Pemasangan PC: the 6-digit code an admin types once per lab PC. */
  function groupCode(code) {
    return code.length === 6 ? `${code.slice(0, 3)} ${code.slice(3)}` : code;
  }

  async function renderPairing() {
    const host = $("#pairing-card");
    if (!host) return;
    const res = await API.getPairing();
    const head = (aside) => `
      <div class="block__head"><h2 class="block__title" id="t-pairing">Pemasangan PC baru</h2>
        ${aside || ""}</div>`;
    if (!res.ok) {
      host.innerHTML = `${head()}<div class="block__body"></div>`;
      host.querySelector(".block__body").appendChild(
        empty({ title: "Kode pemasangan tidak bisa dimuat.", text: res.error }));
      return;
    }

    const open = res.open;
    const state = open
      ? `<span class="status status--online">${icon("dot", "status__icon")}Pendaftaran dibuka</span>`
      : `<span class="status status--offline">${icon("lock", "status__icon")}Pendaftaran ditutup</span>`;
    const digits = String(res.code).split("").map((d) => `<span class="pairing__digit">${esc(d)}</span>`).join("");

    host.innerHTML = `
      ${head(state)}
      <div class="block__body pairing">
        <div class="pairing__code-block">
          <div class="pairing__label" id="pair-code-label">Kode pemasangan</div>
          <div class="pairing__code ${open ? "" : "is-closed"}" aria-labelledby="pair-code-label"
               aria-describedby="pair-code-note" aria-label="${esc(groupCode(res.code))}">${digits}</div>
          <div class="pairing__note" id="pair-code-note">${open
            ? `${res.paired_count} PC sudah terpasang. Satu kode untuk semua PC; tiap PC cukup sekali.`
            : `PC baru ditolak walau kodenya benar. ${res.paired_count} PC yang sudah terpasang tetap terhubung.`}</div>
          <div class="pairing__actions">
            <button class="btn btn--sm" id="pair-rotate" ${open ? "" : "disabled"}>Ganti kode</button>
            <button class="btn btn--sm" id="pair-toggle">${open ? "Tutup pendaftaran" : "Buka pendaftaran"}</button>
          </div>
        </div>
        <div class="pairing__steps">
          <div class="pairing__label">Cara memasang</div>
          <ol class="steps">
            <li>Jalankan installer Lab Sentinel Agent di PC peserta.</li>
            <li>Ketik kode di atas saat diminta, atau jalankan
              <code class="cmd">labsentinel-agent --pair ${esc(res.code)}</code></li>
            <li>PC muncul di <strong>Layar PC</strong> dalam beberapa detik.</li>
          </ol>
          <p class="pairing__hint">Kalau semua PC lab sudah terpasang, tutup pendaftaran supaya tidak ada PC lain yang ikut masuk.</p>
        </div>
      </div>`;

    $("#pair-rotate").addEventListener("click", async (e) => {
      const ok = await confirm({
        title: "Ganti kode pemasangan?",
        message: "Kode lama langsung tidak berlaku. PC yang sudah terpasang tidak terpengaruh.",
        confirmLabel: "Ganti kode",
      });
      if (!ok) return;
      await withBusy(e.currentTarget, async () => {
        const r = await API.rotatePairingCode();
        if (r.ok) { toast("Kode diganti", groupCode(r.code), "ok"); renderPairing(); }
        else toast("Gagal", r.error, "error");
      });
    });

    $("#pair-toggle").addEventListener("click", async (e) => {
      if (open) {
        const ok = await confirm({
          title: "Tutup pendaftaran PC baru?",
          message: "PC yang belum terpasang tidak bisa masuk sampai pendaftaran dibuka lagi. PC yang sudah terpasang tidak terpengaruh.",
          confirmLabel: "Tutup pendaftaran",
        });
        if (!ok) return;
      }
      await withBusy(e.currentTarget, async () => {
        const r = await API.setPairingOpen(!open);
        if (r.ok) {
          toast(r.open ? "Pendaftaran dibuka" : "Pendaftaran ditutup",
                r.open ? "PC baru bisa dipasang dengan kode." : "PC baru tidak bisa masuk.", "ok");
          renderPairing();
        } else toast("Gagal", r.error, "error");
      });
    });

    if (State.focusPairing) {
      State.focusPairing = false;
      host.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }

  function normalizeCat(category) {
    return { "AI chatbot": "ai_chatbot", "situs jawaban": "situs_jawaban",
             "game online": "game_online", "media sosial": "media_sosial",
             "hiburan": "hiburan" }[category] || category;
  }

  /* Session picker + downloads. A report is always about one session, so the
     download buttons wait until one is chosen. */
  async function renderReportActions() {
    const host = $("#report-actions");
    const res = await API.listSessions();
    const sessions = res.ok ? res.sessions.filter((s) => s.started_at) : [];
    if (State.reportSession === undefined) {
      State.reportSession = sessions.length ? sessions[0].id : null;  // newest first
    }
    const opt = (s) => {
      const when = datetime(s.started_at);
      const live = s.status === "active" ? " (berjalan)" : "";
      return `<option value="${s.id}" ${State.reportSession === s.id ? "selected" : ""}>
        ${esc(s.name)} — ${esc(when)}${live}</option>`;
    };
    host.innerHTML = `
      <label class="visually-hidden" for="report-session">Sesi</label>
      <select class="input select" id="report-session">
        <option value="" ${State.reportSession === null ? "selected" : ""}>Semua sesi</option>
        ${sessions.map(opt).join("")}
      </select>
      <button class="btn" id="report-pdf">${icon("download", "btn__icon")}Unduh PDF</button>
      <button class="btn" id="report-xlsx">${icon("download", "btn__icon")}Unduh Excel</button>`;

    const sync = () => {
      const none = State.reportSession === null;
      ["#report-pdf", "#report-xlsx"].forEach((sel) => {
        $(sel).disabled = none;
        $(sel).title = none ? "Pilih satu sesi dulu; laporan dibuat per sesi." : "";
      });
    };
    sync();
    $("#report-session").addEventListener("change", (e) => {
      State.reportSession = e.target.value ? parseInt(e.target.value, 10) : null;
      sync();
      renderViolationRows();
    });
    const download = (fmt) => async (e) => {
      await withBusy(e.currentTarget, async () => {
        const r = await API.exportReport(State.reportSession, fmt);
        if (!r.ok) { toast("Laporan gagal dibuat", r.error, "error"); return; }
        saveBase64(r.b64, r.filename, r.mime);
        toast("Laporan diunduh", r.saved_to ? `Salinannya ada di ${r.saved_to}` : r.filename, "ok");
      });
    };
    $("#report-pdf").addEventListener("click", download("pdf"));
    $("#report-xlsx").addEventListener("click", download("xlsx"));
  }

  function saveBase64(b64, filename, mime) {
    const raw = atob(b64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  async function renderViolations() {
    await renderReportActions();
    await renderViolationRows();
  }

  async function renderViolationRows() {
    const host = $("#violations-table");
    host.innerHTML = '<div class="skeleton" style="height:200px;margin:var(--s-4)"></div>';
    const res = await API.getViolations(State.reportSession || null, 1000);
    if (!res.ok) {
      host.innerHTML = "";
      host.appendChild(empty({ title: "Pelanggaran tidak bisa dimuat.", text: res.error }));
      return;
    }
    if (!res.violations.length) {
      host.innerHTML = "";
      host.appendChild(empty({
        title: State.reportSession ? "Tidak ada pelanggaran di sesi ini." : "Belum ada pelanggaran.",
        text: State.reportSession
          ? "Laporannya tetap bisa diunduh sebagai catatan bahwa sesi berjalan tertib."
          : "",
      }));
      return;
    }
    host.innerHTML = `
      <table class="table table--clickable">
        <thead><tr>
          <th style="width:130px">Waktu</th><th style="width:130px">PC</th>
          <th>Aplikasi atau situs</th><th style="width:230px">Jenis</th>
          <th style="width:110px" class="cell--right">Tangkapan layar</th>
        </tr></thead>
        <tbody>${res.violations.map((v) => `
          <tr data-id="${v.id}">
            <td class="cell--time">${esc(datetime(v.occurred_at))}</td>
            <td class="cell--pc truncate">${esc(v.hostname || v.agent_id)}</td>
            <td>${esc(v.subject)}
              ${v.dry_run ? '<span class="cell__note">dicatat saja, tidak ditindak</span>' : ""}</td>
            <td>${esc(violationLabel(v.kind))}
              ${v.detected_from_title ? '<span class="cell__note">terbaca dari judul jendela</span>' : ""}</td>
            <td class="cell--right">${v.has_screenshot ? "Ada" : "—"}</td>
          </tr>`).join("")}
        </tbody>
      </table>`;
    $$("#violations-table tbody tr").forEach((row) => row.addEventListener("click", () => {
      const v = res.violations.find((x) => String(x.id) === row.dataset.id);
      if (v) openViolation(v);
    }));
  }

  // Plain-language names for audit actions (ADR-0005: no technical terms in
  // the UI). The raw code stays available as a tooltip for support.
  const ACTION_LABEL = {
    "command.pause": "Menjeda PC",
    "command.pause.reapply": "Menjeda ulang PC yang tersambung kembali",
    "command.resume": "Melanjutkan PC",
    "command.lock_screen": "Mengunci layar",
    "command.freeze_input": "Membekukan / mencairkan keyboard & mouse",
    "command.broadcast_message": "Mengirim pesan",
    "command.set_dry_run": "Mengubah mode uji coba",
    "command.set_stream": "Mengatur tampilan layar",
    "rules.update": "Mengubah aturan",
    "session.start": "Memulai sesi",
    "session.end": "Mengakhiri sesi",
    "session.schedule": "Menjadwalkan sesi",
    "session.cancel": "Membatalkan sesi",
    "session.missed": "Sesi terjadwal terlewat",
    "pairing.success": "PC dipasangkan",
    "pairing.failed": "Kode pemasangan salah",
    "pairing.refused": "PC baru ditolak (pendaftaran ditutup)",
    "pairing.rotate": "Membuat kode pemasangan baru",
    "pairing.open": "Membuka pendaftaran PC baru",
    "pairing.close": "Menutup pendaftaran PC baru",
    "pairing.revoke": "Mencabut pemasangan PC",
    "report.export": "Mengunduh laporan sesi",
  };

  function actionLabel(code) {
    if (ACTION_LABEL[code]) return ACTION_LABEL[code];
    if (code.endsWith(".hasil")) {
      const base = ACTION_LABEL[code.slice(0, -".hasil".length)];
      if (base) return `Hasil: ${base.charAt(0).toLowerCase()}${base.slice(1)}`;
    }
    return code;
  }

  // Stored values stay as written; only how they read changes.
  function auditTarget(target) {
    if (!target) return "—";
    return target === "all" || target === "semua" ? "semua PC" : target;
  }

  function auditResult(result) {
    if (!result) return "—";
    return result === "ok" ? "berhasil" : result;
  }

  async function renderAudit() {
    const host = $("#audit-table");
    host.innerHTML = '<div class="skeleton" style="height:200px;margin:var(--s-4)"></div>';
    const res = await API.getAudit();
    if (!res.ok) {
      host.innerHTML = "";
      host.appendChild(empty({ title: "Catatan tidak bisa dimuat.", text: res.error }));
      return;
    }
    if (!res.entries.length) {
      host.innerHTML = "";
      host.appendChild(empty({
        title: "Belum ada catatan.",
        text: "Menjeda PC, mengganti aturan, memulai sesi: semuanya tercatat di sini.",
      }));
      return;
    }
    host.innerHTML = `
      <table class="table">
        <thead><tr>
          <th style="width:130px">Waktu</th><th style="width:110px">Oleh</th>
          <th style="width:340px">Tindakan</th><th>Untuk</th><th style="width:170px">Hasil</th>
        </tr></thead>
        <tbody>${res.entries.map((e) => `
          <tr>
            <td class="cell--time">${esc(datetime(e.ts))}</td>
            <td>${esc(e.actor)}</td>
            <td title="${esc(e.action)}">${esc(actionLabel(e.action))}</td>
            <td class="truncate">${esc(auditTarget(e.target))}</td>
            <td>${esc(auditResult(e.result))}</td>
          </tr>`).join("")}
        </tbody>
      </table>`;
  }

  global.Pages = {
    State, renderOverview, renderGrid, renderRules, renderViolations, renderAudit,
    syncFilterButtons, renderSelectionBar, openBroadcastDialog, stopFrameWatching,
    renderTopbarSession,
  };
})(window);
