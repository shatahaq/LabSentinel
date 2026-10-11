/* Page renderers.
 *
 * The PC grid only fetches a thumbnail for cards that are actually on screen
 * (IntersectionObserver), so a 40+ PC lab stays smooth and agents off-screen
 * are never asked for frames.
 */
(function (global) {
  "use strict";

  const { $, $$, esc, el, icon, statusChip, statusInfo, ago, clock, datetime, fullDate,
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

    renderStats();
    renderDryRunBanner();
    renderSessionPanel(data.upcoming || []);
    renderSessionActions();
    renderTopbarSession();
    renderFeed(data.recent_violations || []);
  }

  function renderStats() {
    const c = State.counts || {};
    const tiles = [
      { key: "online",  label: "Aktif",    value: c.online || 0,  cls: "stat--ok",
        hint: "Terhubung dan diawasi" },
      { key: "paused",  label: "Dijeda",   value: c.paused || 0,  cls: "stat--warn",
        hint: "Dijeda pengawas" },
      { key: "dropped", label: "Terputus", value: c.dropped || 0, cls: "stat--danger",
        hint: "Hilang tanpa pamit" },
      { key: "offline", label: "Mati",     value: c.offline || 0, cls: "",
        hint: "Dimatikan dengan normal" },
    ];
    $("#stats").innerHTML = tiles.map((t) => `
      <button class="stat ${t.cls}" data-stat="${t.key}" style="cursor:pointer;text-align:left;width:100%">
        <div class="stat__label">
          <svg style="width:11px;height:11px" aria-hidden="true">
            <use href="#${statusInfo(t.key).icon}"/></svg>${esc(t.label)}
        </div>
        <div class="stat__value">${t.value}</div>
        <div class="stat__hint">${esc(t.hint)}</div>
      </button>`).join("");

    $$("[data-stat]").forEach((node) => {
      node.addEventListener("click", () => {
        State.filter = node.dataset.stat;
        global.App.goto("monitoring");
        syncFilterButtons();
        renderGrid();
      });
    });
  }

  function renderDryRunBanner() {
    const host = $("#dry-run-banner");
    if (State.rules && State.rules.mode === "free") {
      host.innerHTML = `
        <div class="banner banner--info">
          ${icon("monitor")}
          <div><strong>Mode Bebas.</strong> Layar dan pelanggaran tetap terpantau, tapi tidak ada
            aplikasi yang ditutup atau situs yang diblokir.</div>
        </div>`;
      return;
    }
    if (!State.rules || !State.rules.dry_run) { host.innerHTML = ""; return; }
    host.innerHTML = `
      <div class="banner">
        ${icon("alert")}
        <div>
          <strong>Uji coba.</strong> Pelanggaran hanya dicatat: aplikasi tidak ditutup,
          situs tidak diblokir.
          <div style="margin-top:var(--s-2)">
            <button class="btn btn--sm" id="btn-leave-dryrun">Tegakkan aturan</button>
          </div>
        </div>
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
    if (State.session) {
      const started = State.session.started_at;
      host.innerHTML = `
        ${statusChip(State.globalPaused ? "paused" : "online")}
        <span style="font-weight:var(--weight-medium)">${esc(State.session.name)}</span>
        <span class="tag">${esc(modeLabel(State.session.mode))}</span>
        <span style="color:var(--text-tertiary);font-size:var(--text-sm)">
          mulai ${esc(clock(started))} (${esc(duration(Date.now() / 1000 - (started || 0)))})</span>`;
    } else {
      host.innerHTML = `<span style="color:var(--text-tertiary)">Tidak ada sesi</span>`;
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

  function openStartSessionDialog() {
    openDialog({
      title: "Mulai sesi",
      subtitle: "Pelanggaran selama sesi masuk ke laporan sesi ini.",
      build: (body) => {
        body.innerHTML = `
          <label style="display:block;margin-bottom:var(--s-4)">
            <div style="font-weight:var(--weight-medium);margin-bottom:var(--s-1)">Nama sesi</div>
            <input class="input" id="sess-name" data-autofocus
                   placeholder="UTS Algoritma, Kelas A">
          </label>
          <div style="font-weight:var(--weight-medium);margin-bottom:var(--s-2)">Mode</div>
          <div class="mode-row" id="sess-modes"></div>`;
        const modes = ["exam", "lab", "free"];
        const descs = {
          exam: ["Ujian", "Paling ketat", "Hanya Chrome dan Edge. Semua kategori situs dan situs tambahan diblokir."],
          lab: ["Praktikum", "Sedang", "Browser dan aplikasi yang diizinkan. Kategori pilihan dan situs tambahan diblokir."],
          free: ["Bebas", "Hanya memantau", "Tidak ada yang ditutup atau diblokir. Pelanggaran tetap dicatat."],
        };
        $("#sess-modes", body).innerHTML = modes.map((m, i) => `
          <button class="mode" data-mode="${m}" aria-pressed="${i === 0}">
            <div class="mode__summary">${esc(descs[m][1])}</div>
            <div class="mode__name">${esc(descs[m][0])}</div>
            <div class="mode__desc">${esc(descs[m][2])}</div>
          </button>`).join("");
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
          const mode = ($('[data-mode][aria-pressed="true"]') || {}).dataset.mode || "lab";
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
    if (!State.session && !upcoming.length) {
      host.innerHTML = "";
      host.appendChild(empty({
        iconName: "clock",
        title: "Tidak ada sesi berjalan",
        text: "Pelanggaran tetap dicatat, tapi tidak masuk ke laporan sesi mana pun. Mulai sesi saat praktikum atau ujian dimulai.",
        actionLabel: "Mulai sesi",
        onAction: openStartSessionDialog,
      }));
      return;
    }
    const parts = [];
    if (State.session) {
      parts.push(`
        <div style="display:flex;align-items:center;gap:var(--s-2);margin-bottom:var(--s-3)">
          ${statusChip("online")}
          <div>
            <div style="font-weight:var(--weight-medium)">${esc(State.session.name)}</div>
            <div style="font-size:var(--text-sm);color:var(--text-secondary)">
              Mode ${esc(modeLabel(State.session.mode))} · mulai ${esc(clock(State.session.started_at))}
            </div>
          </div>
        </div>`);
    }
    if (upcoming.length) {
      parts.push(`<div class="nav-section" style="margin-left:0">Terjadwal</div>`);
      parts.push(upcoming.map((s) => `
        <div style="display:flex;gap:var(--s-2);align-items:center;padding:6px 0;border-top:1px solid var(--border)">
          ${icon("clock", "")}
          <div style="min-width:0">
            <div class="truncate" style="font-weight:var(--weight-medium)">${esc(s.name)}</div>
            <div style="font-size:var(--text-sm);color:var(--text-secondary)">
              ${esc(fullDate(s.scheduled_start))}</div>
          </div>
        </div>`).join(""));
    }
    host.innerHTML = parts.join("");
  }

  function renderFeed(violations) {
    const host = $("#recent-feed");
    host.innerHTML = "";
    if (!violations.length) {
      host.appendChild(empty({
        iconName: "check",
        title: "Belum ada pelanggaran",
        text: "Setiap pelanggaran muncul di sini dengan tangkapan layarnya.",
      }));
      return;
    }
    violations.forEach((v) => host.appendChild(feedItem(v)));
  }

  function feedItem(v) {
    const muted = v.dry_run;
    const node = el(`
      <div class="feed__item" role="button" tabindex="0">
        <div class="feed__icon ${muted ? "feed__icon--muted" : ""}">
          ${icon({ site_title: "globe", agent_tamper: "alert" }[v.kind] || "app")}
        </div>
        <div class="feed__body">
          <div class="feed__title truncate">${esc(v.subject)}</div>
          <div class="feed__sub">
            <span>${esc(v.hostname || v.agent_id)}</span>
            <span class="tag">${esc(violationLabel(v.kind))}</span>
            ${v.detected_from_title ? '<span class="tag tag--warn">dari judul jendela</span>' : ""}
            ${v.dry_run ? '<span class="tag tag--muted">dicatat saja</span>' : ""}
          </div>
        </div>
        <div class="feed__time">${esc(ago(v.occurred_at))}</div>
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
        body.innerHTML = `
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:var(--s-4)">
            <span class="tag">${esc(violationLabel(v.kind))}</span>
            ${v.detected_from_title
              ? '<span class="tag tag--warn">terbaca dari judul jendela, bisa keliru</span>' : ""}
            ${v.dry_run ? '<span class="tag tag--muted">dicatat saja, tidak ditindak</span>' : ""}
          </div>
          ${v.detail ? `<p style="color:var(--text-secondary);margin-bottom:var(--s-4)">${esc(v.detail)}</p>` : ""}
          <div id="shot-host"></div>`;
        const shotHost = $("#shot-host", body);
        if (!v.has_screenshot) {
          shotHost.appendChild(empty({
            iconName: "desktop-off", title: "Tidak ada tangkapan layar",
            text: "Pelanggaran ini tercatat tanpa tangkapan layar.",
          }));
          return;
        }
        shotHost.innerHTML = '<div class="skeleton" style="aspect-ratio:16/9;width:100%"></div>';
        API.getScreenshot(v.id).then((res) => {
          if (res.ok && res.image) {
            shotHost.innerHTML =
              `<img src="data:image/jpeg;base64,${res.image}" alt="Bukti tangkapan layar"
                    style="width:100%;border-radius:var(--radius);border:1px solid var(--border)">`;
          } else {
            shotHost.innerHTML = "";
            shotHost.appendChild(empty({
              iconName: "desktop-off", title: "Tangkapan layar tidak bisa dibuka",
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
        iconName: "monitor",
        title: "Belum ada PC terpasang",
        text: "Pasang agent di PC peserta, lalu masukkan kode pemasangan 6 digit. PC menemukan dashboard ini sendiri di jaringan lab; alamat IP tidak perlu diisi.",
        actionLabel: "Lihat kode pemasangan",
        onAction: () => { State.focusPairing = true; global.App.goto("aturan"); },
      }));
      return;
    }
    if (!list.length) {
      host.appendChild(empty({
        iconName: "search",
        title: "Tidak ada PC yang cocok",
        text: "Ubah kata pencarian atau saringan status.",
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
    $("#monitoring-sub").textContent =
      `${list.length} dari ${State.agents.length} PC. Klik PC untuk melihat layarnya lebih besar.`;
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
            ${icon({ online: "monitor", paused: "pause" }[a.status] || "desktop-off")}
            <span>${esc(a.status === "online" ? "Memuat layar…" : statusInfo(a.status).label)}</span>
          </div>
          ${a.desktop_access === false
            ? '<div class="pc__overlay"><span class="pc__tag">layar tidak terjangkau</span></div>'
            : a.indicator_visible === false
              ? '<div class="pc__overlay"><span class="pc__tag">indikator disembunyikan</span></div>' : ""}
          ${a.input_frozen ? `<span class="pc__frozen">${icon("frozen", "")}Input dibekukan</span>` : ""}
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
                   ${icon("alert-filled", "")}${a.violation_count}</span>`
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
        <span style="font-size:var(--text-sm);color:var(--text-secondary)">
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
          <p style="margin-top:var(--s-2);font-size:var(--text-sm);color:var(--text-secondary)">
            Muncul sebagai notifikasi kecil di layar peserta; jendela yang sedang dipakai tidak berpindah.</p>`;
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
        const pause = el(`<button class="btn">${icon("pause", "btn__icon")}Jeda</button>`);
        const resume = el(`<button class="btn">${icon("play", "btn__icon")}Lanjutkan</button>`);
        const lock = el(`<button class="btn">${icon("lock", "btn__icon")}Kunci layar</button>`);
        const freeze = el('<button class="btn"></button>');
        paintFreeze = () => {
          const frozen = !!agent.input_frozen;
          freeze.innerHTML = `${icon("frozen", "btn__icon")}${frozen ? "Cairkan input" : "Bekukan input"}`;
          freeze.classList.toggle("btn--warn", frozen);
          // No desktop worker means no way to freeze this PC right now.
          freeze.disabled = agent.desktop_access === false;
          freeze.title = freeze.disabled ? "Layar PC ini sedang tidak terjangkau" : "";
        };
        paintFreeze();
        const done = el('<button class="btn btn--primary">Tutup</button>');
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
        foot.append(pause, resume, lock, freeze, done);
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
    const facts = [
      ["Status", statusInfo(agent.status).label],
      ["Pelanggaran", String(agent.violation_count || 0)],
      ["Jendela aktif", agent.active_window_title || "—"],
      ["Versi aturan", String(agent.rule_revision)],
      ["CPU agent", `${(agent.cpu_percent || 0).toFixed(1)}%`],
      ["RAM agent", `${Math.round(agent.mem_mb || 0)} MB`],
      ["Kabar terakhir", ago(agent.last_heartbeat)],
      ["Ikon di layar peserta", agent.indicator_visible === false ? "Disembunyikan" : "Terlihat"],
      ["Akses layar", agent.desktop_access === false ? "Tidak terjangkau" : "Normal"],
      ["Keyboard & mouse", agent.input_frozen ? "Dibekukan" : "Normal"],
    ];
    const host = $("#detail-facts");
    if (host) {
      host.innerHTML = facts.map(([k, v]) => `
        <div><div class="fact__label">${esc(k)}</div>
        <div class="fact__value truncate">${esc(v)}</div></div>`).join("");
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
      host.appendChild(empty({ iconName: "alert", title: "Aturan tidak bisa dimuat", text: res.error }));
      return;
    }
    const rules = res.rules;
    State.rules = rules;

    host.innerHTML = `
      <div class="card" style="margin-bottom:var(--s-4)">
        <div class="card__head"><span class="card__title">Mode</span>
          <div class="card__actions"><span class="tag">Versi aturan ${rules.revision}</span></div>
        </div>
        <div class="card__body"><div class="mode-row" id="mode-row"></div></div>
      </div>

      <div class="grid-2">
        <div class="card">
          <div class="card__head"><span class="card__title">Aplikasi yang diizinkan</span></div>
          <div class="card__body">
            ${appsNote(rules)}
            <div class="chip-input" id="wl-chips"></div>
            <div style="display:flex;gap:var(--s-2);margin-top:var(--s-3)">
              <input class="input" id="wl-new" placeholder="code.exe" style="flex:1"
                     aria-label="Nama berkas aplikasi">
              <button class="btn" id="wl-add">Tambah</button>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card__head"><span class="card__title">Situs yang diblokir</span>
            <div class="card__actions"><span class="tag" id="site-count">${rules.site_count} situs</span></div>
          </div>
          <div class="card__body">
            ${sitesNote(rules)}
            <div class="check-list" id="cat-list"></div>
            <div style="font-weight:var(--weight-medium);margin:var(--s-4) 0 var(--s-1)">Situs tambahan</div>
            <p style="color:var(--text-secondary);margin-bottom:var(--s-3);font-size:var(--text-sm)">
              Tempel alamatnya. Subdomain ikut diblokir; berlaku di Praktikum dan Ujian.</p>
            <div class="chip-input" id="site-chips"></div>
            <div style="display:flex;gap:var(--s-2);margin-top:var(--s-3)">
              <input class="input" id="site-new" placeholder="tiktok.com" style="flex:1"
                     aria-label="Alamat situs">
              <button class="btn" id="site-add">Tambah</button>
            </div>
          </div>
        </div>
      </div>

      <div class="card" style="margin-top:var(--s-4)">
        <div class="card__head"><span class="card__title">Penindakan</span></div>
        <div class="card__body">
          <label class="switch" style="margin-bottom:var(--s-4)">
            <input type="checkbox" id="opt-dryrun" ${rules.dry_run ? "checked" : ""}>
            <span>
              <div style="font-weight:var(--weight-medium)">Uji coba: hanya catat, jangan tindak</div>
              <div style="font-size:var(--text-sm);color:var(--text-secondary)">
                Aplikasi tidak ditutup dan situs tidak diblokir; pelanggaran tetap masuk laporan.
                ${rules.mode === "free" ? "Mode Bebas memang tidak menindak apa pun." : ""}</div>
            </span>
          </label>
          <label style="display:block;max-width:320px">
            <div style="font-weight:var(--weight-medium);margin-bottom:var(--s-1)">
              Waktu peringatan sebelum aplikasi ditutup</div>
            <div style="display:flex;gap:var(--s-2);align-items:center">
              <input class="input" type="number" id="opt-grace" min="0" max="60"
                     value="${rules.close_grace_seconds}" style="width:90px">
              <span style="color:var(--text-secondary)">detik</span>
              <button class="btn btn--sm" id="opt-grace-save">Simpan</button>
            </div>
            <div style="font-size:var(--text-sm);color:var(--text-secondary);margin-top:4px">
              Peserta sempat menyimpan pekerjaannya dulu.</div>
          </label>
        </div>
      </div>

      <div class="card" id="pairing-card" style="margin-top:var(--s-4)"></div>`;
    renderPairing();

    // modes
    $("#mode-row").innerHTML = res.modes.map((m) => `
      <button class="mode" data-mode="${esc(m.key)}" aria-pressed="${rules.mode === m.key}">
        <div class="mode__summary">${esc(m.summary)}</div>
        <div class="mode__name">${esc(m.label)}</div>
        <div class="mode__desc">${esc(m.description)}</div>
      </button>`).join("");
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
        <span>
          <div class="check__name">${esc(c.label)} <span class="tag">${c.count} situs</span></div>
          <div class="check__desc">${esc(c.description)}</div>
        </span>
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
      $("#site-count").textContent = `${next.site_count} situs`;
    }
    function paintSites() {
      $("#site-chips").innerHTML = rules.custom_sites.length
        ? rules.custom_sites.map((p) => `
            <span class="chip">${esc(p)}
              <button data-remove-site="${esc(p)}" aria-label="Hapus ${esc(p)}">${icon("x", "")}</button>
            </span>`).join("")
        : '<span style="color:var(--text-tertiary);font-size:var(--text-sm);padding:2px 4px">Belum ada</span>';
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
      return `<div class="banner banner--info">${icon("app")}<div>
        <strong>Ujian: hanya browser.</strong> Daftar ini tidak berlaku sekarang dan
        disimpan untuk Praktikum.</div></div>`;
    }
    const action = rules.acts ? "ditutup" : "hanya dicatat";
    return `<p style="color:var(--text-secondary);margin-bottom:var(--s-3);font-size:var(--text-sm)">
      Aplikasi lain ${action}. Chrome, Edge, dan Windows Explorer selalu boleh.</p>`;
  }

  function sitesNote(rules) {
    if (rules.mode === "exam") {
      return `<div class="banner banner--info">${icon("globe")}<div>
        <strong>Ujian: semua kategori diblokir.</strong> Centang di bawah berlaku untuk
        Praktikum.</div></div>`;
    }
    if (rules.mode === "free") {
      return `<div class="banner banner--info">${icon("globe")}<div>
        <strong>Bebas: tidak ada yang diblokir.</strong> Daftar ini dipakai saat Praktikum
        dan Ujian.</div></div>`;
    }
    return "";
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
    if (!res.ok) {
      host.innerHTML = `
        <div class="card__head"><span class="card__title">Pemasangan PC baru</span></div>
        <div class="card__body"></div>`;
      host.querySelector(".card__body").appendChild(
        empty({ iconName: "alert", title: "Kode pemasangan tidak bisa dimuat", text: res.error }));
      return;
    }

    const open = res.open;
    const state = open
      ? `<span class="status status--online">${icon("dot", "status__icon")}Pendaftaran dibuka</span>`
      : `<span class="status status--offline">${icon("lock", "status__icon")}Pendaftaran ditutup</span>`;

    host.innerHTML = `
      <div class="card__head">
        <span class="card__title">Pemasangan PC baru</span>
        <div class="card__actions">${state}</div>
      </div>
      <div class="card__body pairing">
        <div class="pairing__code-block">
          <div class="pairing__label" id="pair-code-label">Kode pemasangan</div>
          <div class="pairing__code ${open ? "" : "is-closed"}" aria-labelledby="pair-code-label"
               aria-describedby="pair-code-note">${esc(groupCode(res.code))}</div>
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
            <li>Ketik kode ini saat diminta, atau jalankan
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
      host.appendChild(empty({ iconName: "alert", title: "Gagal memuat", text: res.error }));
      return;
    }
    if (!res.violations.length) {
      host.innerHTML = "";
      host.appendChild(empty({
        iconName: "check",
        title: State.reportSession ? "Tidak ada pelanggaran di sesi ini" : "Belum ada pelanggaran",
        text: State.reportSession
          ? "Laporannya tetap bisa diunduh sebagai catatan bahwa sesi berjalan tertib."
          : "",
      }));
      return;
    }
    host.innerHTML = `
      <table class="table table--clickable">
        <thead><tr>
          <th style="width:130px">Waktu</th><th style="width:150px">PC</th>
          <th>Aplikasi atau situs</th><th style="width:190px">Jenis</th>
          <th style="width:110px" class="cell--right">Bukti layar</th>
        </tr></thead>
        <tbody>${res.violations.map((v) => `
          <tr data-id="${v.id}">
            <td class="num">${esc(datetime(v.occurred_at))}</td>
            <td class="truncate">${esc(v.hostname || v.agent_id)}</td>
            <td class="truncate">${esc(v.subject)}
              ${v.dry_run ? '<span class="tag tag--muted">dicatat saja</span>' : ""}</td>
            <td>${esc(violationLabel(v.kind))}
              ${v.detected_from_title ? '<span class="tag tag--warn">judul jendela</span>' : ""}</td>
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
      host.appendChild(empty({ iconName: "alert", title: "Gagal memuat", text: res.error }));
      return;
    }
    if (!res.entries.length) {
      host.innerHTML = "";
      host.appendChild(empty({
        iconName: "history", title: "Belum ada catatan",
        text: "Menjeda PC, mengganti aturan, memulai sesi: semuanya tercatat di sini.",
      }));
      return;
    }
    host.innerHTML = `
      <table class="table">
        <thead><tr>
          <th style="width:150px">Waktu</th><th style="width:130px">Oleh</th>
          <th style="width:300px">Tindakan</th><th>Untuk</th><th style="width:170px">Hasil</th>
        </tr></thead>
        <tbody>${res.entries.map((e) => `
          <tr>
            <td class="num">${esc(datetime(e.ts))}</td>
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
