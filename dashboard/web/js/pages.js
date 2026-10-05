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
        hint: "Sedang diawasi" },
      { key: "paused",  label: "Dijeda",   value: c.paused || 0,  cls: "stat--warn",
        hint: "Pemantauan dihentikan sementara" },
      { key: "dropped", label: "Terputus", value: c.dropped || 0, cls: "stat--danger",
        hint: "Tidak mengirim kabar" },
      { key: "offline", label: "Offline",  value: c.offline || 0, cls: "",
        hint: "Belum menyala" },
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
          <div><strong>Mode Bebas: hanya memantau.</strong>
            Layar PC tetap terlihat dan aplikasi di luar daftar tetap dicatat, tetapi
            tidak ada aplikasi yang ditutup atau situs yang diblokir.</div>
        </div>`;
      return;
    }
    if (!State.rules || !State.rules.dry_run) { host.innerHTML = ""; return; }
    host.innerHTML = `
      <div class="banner">
        ${icon("alert")}
        <div>
          <strong>Mode uji coba aktif.</strong>
          Pelanggaran dicatat, tetapi aplikasi <em>tidak</em> ditutup dan situs
          <em>tidak</em> diblokir. Cocok untuk mencoba aturan sebelum ujian.
          <div style="margin-top:var(--s-2)">
            <button class="btn btn--sm" id="btn-leave-dryrun">Mulai menegakkan aturan</button>
          </div>
        </div>
      </div>`;
    $("#btn-leave-dryrun").addEventListener("click", async (ev) => {
      const ok = await confirm({
        title: "Mulai menegakkan aturan?",
        message: "Mulai sekarang, aplikasi di luar daftar izin akan ditutup dan situs terlarang akan diblokir di semua PC.",
        confirmLabel: "Ya, tegakkan aturan",
      });
      if (!ok) return;
      await withBusy(ev.currentTarget, async () => {
        const res = await API.setDryRun(false);
        if (res.ok) { toast("Aturan ditegakkan", `Dikirim ke ${res.reached} PC.`, "ok"); global.App.refresh(); }
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
          berjalan ${esc(duration(Date.now() / 1000 - (started || 0)))}</span>`;
    } else {
      host.innerHTML = `<span style="color:var(--text-tertiary)">Belum ada sesi berjalan</span>`;
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
        title: "Akhiri sesi sekarang?",
        message: `Sesi "${State.session.name}" akan ditutup dan pemantauan dijeda di semua PC.`,
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
      title: "Mulai sesi baru",
      subtitle: "Sesi mengelompokkan pelanggaran agar laporannya rapi.",
      build: (body) => {
        body.innerHTML = `
          <label style="display:block;margin-bottom:var(--s-4)">
            <div style="font-weight:var(--weight-medium);margin-bottom:var(--s-1)">Nama sesi</div>
            <input class="input" id="sess-name" data-autofocus
                   placeholder="Contoh: Ujian Algoritma - Kelas A">
          </label>
          <div style="font-weight:var(--weight-medium);margin-bottom:var(--s-2)">Mode</div>
          <div class="mode-row" id="sess-modes"></div>`;
        const modes = ["exam", "lab", "free"];
        const descs = {
          exam: ["Ujian", "Paling ketat", "Hanya browser. Semua kategori situs dan situs tambahan diblokir."],
          lab: ["Praktikum", "Sedang", "Browser + aplikasi mata kuliah. Situs pilihan diblokir."],
          free: ["Bebas", "Hanya memantau", "Tidak ada yang ditutup atau diblokir; tetap dicatat."],
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
          if (!name) { toast("Nama sesi wajib diisi", "Beri nama agar laporan mudah dicari.", "warn"); return; }
          const mode = ($('[data-mode][aria-pressed="true"]') || {}).dataset.mode || "lab";
          await withBusy(go, async () => {
            const res = await API.startSession(name, mode);
            if (res.ok) { close(); toast("Sesi dimulai", `${name} — mode ${modeLabel(mode)}.`, "ok"); global.App.refresh(); }
            else toast("Gagal memulai sesi", res.error, "error");
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
        title: "Belum ada sesi",
        text: "Mulai sesi saat praktikum atau ujian dimulai, agar pelanggaran tercatat per sesi dan mudah dilaporkan.",
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
        text: "Pelanggaran akan muncul di sini begitu terjadi, lengkap dengan bukti tangkapan layar.",
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
            ${v.dry_run ? '<span class="tag tag--muted">hanya dicatat</span>' : ""}
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
              ? '<span class="tag tag--warn">terdeteksi dari judul jendela (akurasi terbatas)</span>' : ""}
            ${v.dry_run ? '<span class="tag tag--muted">hanya dicatat — tidak ditindak</span>' : ""}
          </div>
          ${v.detail ? `<p style="color:var(--text-secondary);margin-bottom:var(--s-4)">${esc(v.detail)}</p>` : ""}
          <div id="shot-host"></div>`;
        const shotHost = $("#shot-host", body);
        if (!v.has_screenshot) {
          shotHost.appendChild(empty({
            iconName: "eye-off", title: "Tanpa bukti layar",
            text: "Tangkapan layar tidak tersedia untuk pelanggaran ini.",
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
              iconName: "eye-off", title: "Bukti tidak dapat dibuka",
              text: res.error || "Berkas mungkin sudah dihapus oleh kebijakan retensi.",
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
  const frameTimers = new Map();

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
    stopFrameWatching();
    host.innerHTML = "";

    if (!State.agents.length) {
      host.appendChild(empty({
        iconName: "monitor",
        title: "Belum ada PC yang terhubung",
        text: "Pasang agent di PC peserta dengan kode pemasangan 6 digit. Agent menemukan dashboard ini sendiri lewat jaringan lab — alamat IP tidak perlu diatur.",
        actionLabel: "Lihat kode pemasangan",
        onAction: () => { State.focusPairing = true; global.App.goto("aturan"); },
      }));
      return;
    }
    if (!list.length) {
      host.appendChild(empty({
        iconName: "search",
        title: "Tidak ada PC yang cocok",
        text: "Coba ubah kata pencarian atau pilih saringan status yang lain.",
        actionLabel: "Tampilkan semua",
        onAction: () => { State.filter = "all"; State.search = ""; $("#pc-search").value = ""; syncFilterButtons(); renderGrid(); },
      }));
      return;
    }

    list.forEach((a) => host.appendChild(pcCard(a)));
    startFrameWatching();
    $("#monitoring-sub").textContent =
      `${list.length} dari ${State.agents.length} PC ditampilkan. Klik satu PC untuk melihat layarnya lebih besar.`;
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
            <span>${esc(a.status === "online" ? "Memuat layar..." : statusInfo(a.status).label)}</span>
          </div>
          ${a.desktop_access === false
            ? '<div class="pc__overlay"><span class="pc__tag">layar tidak terjangkau</span></div>'
            : a.indicator_visible === false
              ? '<div class="pc__overlay"><span class="pc__tag">indikator disembunyikan</span></div>' : ""}
        </div>
        <div class="pc__meta">
          <div style="min-width:0">
            <div class="pc__name truncate">${esc(a.hostname || a.agent_id)}</div>
            <div class="pc__sub truncate">${esc(a.username || "—")}</div>
          </div>
          <div class="pc__right">
            ${a.violation_count > 0
              ? `<span class="pc__count ${recent ? "" : "pc__count--past"}"
                       title="${a.violation_count} pelanggaran${recent ? ", terbaru barusan" : ""}">
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

  /* Only poll frames for cards actually on screen. */
  function startFrameWatching() {
    frameObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        const id = entry.target.dataset.agent;
        if (entry.isIntersecting) scheduleFrame(id, entry.target);
        else clearFrame(id);
      });
    }, { rootMargin: "120px" });
    $$("#pc-grid .pc").forEach((n) => frameObserver.observe(n));
  }

  function stopFrameWatching() {
    if (frameObserver) { frameObserver.disconnect(); frameObserver = null; }
    frameTimers.forEach((t) => clearInterval(t));
    frameTimers.clear();
  }

  function clearFrame(id) {
    const timer = frameTimers.get(id);
    if (timer) { clearInterval(timer); frameTimers.delete(id); }
  }

  function scheduleFrame(id, card) {
    if (frameTimers.has(id)) return;
    const paint = async () => {
      const res = await API.getFrame(id);
      const screen = card.querySelector("[data-screen]");
      if (!screen || !res.ok) return;
      if (!res.frame) return;
      let img = screen.querySelector("img");
      if (!img) {
        img = document.createElement("img");
        img.alt = "";
        const ph = screen.querySelector(".pc__placeholder");
        if (ph) ph.remove();
        screen.insertBefore(img, screen.firstChild);
      }
      img.src = `data:image/jpeg;base64,${res.frame.jpeg_b64}`;
    };
    paint();
    frameTimers.set(id, setInterval(paint, 1200));
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
          ${n} PC dipilih</span>
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
      $("#btn-select-mode").textContent = "Pilih beberapa PC";
      renderSelectionBar(); renderGrid();
    });
  }

  async function massAction(button, verb, run, count, danger) {
    const ok = await confirm({
      title: `${verb} di ${count} PC?`,
      message: `Tindakan ini dikirim ke ${count} PC sekaligus.`,
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
      title: "Kirim pesan ke peserta",
      subtitle: ids && ids.length ? `${ids.length} PC terpilih` : "Semua PC",
      build: (body) => {
        body.innerHTML = `
          <textarea class="input" id="bc-text" data-autofocus maxlength="500"
                    placeholder="Contoh: Ujian dimulai 5 menit lagi. Tutup semua catatan."></textarea>
          <p style="margin-top:var(--s-2);font-size:var(--text-sm);color:var(--text-secondary)">
            Pesan muncul sebagai pemberitahuan kecil di layar peserta dan tidak merebut fokus.</p>`;
      },
      footer: (foot, close) => {
        const cancel = el('<button class="btn">Batal</button>');
        const send = el('<button class="btn btn--primary">Kirim</button>');
        cancel.addEventListener("click", close);
        send.addEventListener("click", async () => {
          const text = $("#bc-text").value.trim();
          if (!text) { toast("Pesan kosong", "Tulis pesan terlebih dahulu.", "warn"); return; }
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
  let detailTimer = null;

  function openPc(agentId) {
    const agent = State.agents.find((a) => a.agent_id === agentId);
    if (!agent) return;
    State.openAgent = agentId;
    API.setViewing(true, agentId);

    const dialog = openDialog({
      title: agent.hostname || agentId,
      subtitle: `${agent.username || "—"} · ${agent.remote_addr || ""}`,
      wide: true,
      build: (body) => {
        body.innerHTML = `
          <div class="detail__screen" id="detail-screen">
            <div class="pc__placeholder">${icon("monitor")}<span>Memuat layar...</span></div>
          </div>
          <div class="detail__facts" id="detail-facts"></div>`;
        paintDetail(agent);
        detailTimer = setInterval(async () => {
          const res = await API.getFrame(agentId);
          const screen = $("#detail-screen");
          if (!screen || !res.ok || !res.frame) return;
          let img = screen.querySelector("img");
          if (!img) {
            img = document.createElement("img");
            img.alt = "Layar peserta";
            screen.innerHTML = "";
            screen.appendChild(img);
          }
          img.src = `data:image/jpeg;base64,${res.frame.jpeg_b64}`;
        }, 400);
      },
      footer: (foot, close) => {
        const pause = el('<button class="btn">Jeda PC ini</button>');
        const resume = el('<button class="btn">Lanjutkan</button>');
        const lock = el('<button class="btn">Kunci layar</button>');
        const done = el('<button class="btn btn--primary">Tutup</button>');
        pause.addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
          reportToast(await API.pause([agentId]), "Jeda"); global.App.refresh();
        }));
        resume.addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
          reportToast(await API.resume([agentId]), "Lanjutkan"); global.App.refresh();
        }));
        lock.addEventListener("click", async (e) => {
          const ok = await confirm({
            title: "Kunci layar PC ini?",
            message: "Peserta harus memasukkan kata sandi Windows untuk masuk kembali.",
            confirmLabel: "Kunci layar", danger: true,
          });
          if (!ok) return;
          await withBusy(e.currentTarget, async () => reportToast(await API.lockScreens([agentId]), "Kunci layar"));
        });
        done.addEventListener("click", close);
        foot.append(pause, resume, lock, done);
      },
    });

    // Stop the hi-res stream as soon as the admin closes the view.
    const scrim = $("#scrim");
    const observer = new MutationObserver(() => {
      if (scrim.hidden) {
        observer.disconnect();
        if (detailTimer) { clearInterval(detailTimer); detailTimer = null; }
        State.openAgent = null;
        API.setViewing(global.App.currentPage() === "monitoring", null);
      }
    });
    observer.observe(scrim, { attributes: true, attributeFilter: ["hidden"] });
    return dialog;
  }

  function paintDetail(agent) {
    const facts = [
      ["Status", statusInfo(agent.status).label],
      ["Pelanggaran", String(agent.violation_count || 0)],
      ["Jendela aktif", agent.active_window_title || "—"],
      ["Aturan versi", String(agent.rule_revision)],
      ["CPU agent", `${(agent.cpu_percent || 0).toFixed(1)}%`],
      ["RAM agent", `${Math.round(agent.mem_mb || 0)} MB`],
      ["Kabar terakhir", ago(agent.last_heartbeat)],
      ["Indikator peserta", agent.indicator_visible === false ? "Tidak terlihat" : "Terlihat"],
      ["Akses layar", agent.desktop_access === false
        ? "Tidak terjangkau — layar, judul jendela, dan penutupan aplikasi tidak berjalan"
        : "Normal"],
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
      host.appendChild(empty({ iconName: "alert", title: "Gagal memuat aturan", text: res.error }));
      return;
    }
    const rules = res.rules;
    State.rules = rules;

    host.innerHTML = `
      <div class="card" style="margin-bottom:var(--s-4)">
        <div class="card__head"><span class="card__title">Mode</span>
          <div class="card__actions"><span class="tag">versi ${rules.revision}</span></div>
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
              <input class="input" id="wl-new" placeholder="contoh: code.exe" style="flex:1"
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
              Diblokir di mode Praktikum dan Ujian. Tempel alamatnya; seluruh situs ikut
              diblokir, termasuk subdomainnya.</p>
            <div class="chip-input" id="site-chips"></div>
            <div style="display:flex;gap:var(--s-2);margin-top:var(--s-3)">
              <input class="input" id="site-new" placeholder="contoh: tiktok.com" style="flex:1"
                     aria-label="Alamat situs">
              <button class="btn" id="site-add">Tambah</button>
            </div>
          </div>
        </div>
      </div>

      <div class="card" style="margin-top:var(--s-4)">
        <div class="card__head"><span class="card__title">Perilaku</span></div>
        <div class="card__body">
          <label class="switch" style="margin-bottom:var(--s-4)">
            <input type="checkbox" id="opt-dryrun" ${rules.dry_run ? "checked" : ""}>
            <span>
              <div style="font-weight:var(--weight-medium)">Mode uji coba (hanya mencatat)</div>
              <div style="font-size:var(--text-sm);color:var(--text-secondary)">
                Pelanggaran dicatat, tapi aplikasi tidak ditutup dan situs tidak diblokir.
                ${rules.mode === "free" ? "Di mode Bebas tidak ada yang ditindak, apa pun pilihan ini." : ""}</div>
            </span>
          </label>
          <label style="display:block;max-width:320px">
            <div style="font-weight:var(--weight-medium);margin-bottom:var(--s-1)">
              Jeda peringatan sebelum menutup aplikasi</div>
            <div style="display:flex;gap:var(--s-2);align-items:center">
              <input class="input" type="number" id="opt-grace" min="0" max="60"
                     value="${rules.close_grace_seconds}" style="width:90px">
              <span style="color:var(--text-secondary)">detik</span>
              <button class="btn btn--sm" id="opt-grace-save">Simpan</button>
            </div>
            <div style="font-size:var(--text-sm);color:var(--text-secondary);margin-top:4px">
              Peserta diberi waktu menyimpan pekerjaan sebelum aplikasi ditutup.</div>
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
          title: `Ganti mode ke ${label}?`,
          message: "Aturan baru langsung dikirim ke semua PC yang terhubung.",
          confirmLabel: "Ganti mode",
        });
        if (!ok) return;
        await withBusy(btn, async () => {
          const r = await API.setMode(btn.dataset.mode);
          if (r.ok) { toast("Mode diganti", `Dikirim ke ${r.reached} PC.`, "ok"); renderRules(); global.App.refresh(); }
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
        if (r.ok) { rules.app_whitelist = r.rules.app_whitelist; paintChips(); toast("Aplikasi dihapus", "", "ok"); }
        else toast("Gagal", r.error, "error");
      }));
    }
    paintChips();
    const addApp = async () => {
      const name = $("#wl-new").value.trim().toLowerCase();
      if (!name) return;
      if (!name.endsWith(".exe")) { toast("Nama tidak valid", "Tulis nama berkas lengkap, contoh: code.exe", "warn"); return; }
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
      if (r.ok) { paintSiteCount(r.rules); toast("Daftar situs diperbarui", siteCountText(r.rules), "ok"); }
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
              added.length ? added.join(", ") : "Situs itu sudah diblokir.", added.length ? "ok" : "info");
      } else toast("Alamat tidak valid", r.error, "warn");
    };
    $("#site-add").addEventListener("click", addSite);
    $("#site-new").addEventListener("keydown", (e) => { if (e.key === "Enter") addSite(); });

    // behaviour
    $("#opt-dryrun").addEventListener("change", async (e) => {
      const on = e.target.checked;
      const r = await API.setDryRun(on);
      if (r.ok) { toast(on ? "Mode uji coba aktif" : "Aturan ditegakkan", "", "ok"); global.App.refresh(); }
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
        <strong>Mode Ujian: hanya browser.</strong> Daftar ini tidak berlaku saat Ujian;
        disimpan untuk Praktikum.</div></div>`;
    }
    const action = rules.acts ? "akan ditutup otomatis" : "hanya dicatat, tidak ditutup";
    return `<p style="color:var(--text-secondary);margin-bottom:var(--s-3);font-size:var(--text-sm)">
      Aplikasi di luar daftar ini ${action}. Browser dan Windows Explorer selalu diizinkan
      dan tidak dapat dihapus.</p>`;
  }

  function sitesNote(rules) {
    if (rules.mode === "exam") {
      return `<div class="banner banner--info">${icon("globe")}<div>
        <strong>Mode Ujian: semua kategori diblokir,</strong> ditambah situs tambahan.
        Pilihan kategori di bawah dipakai saat Praktikum.</div></div>`;
    }
    if (rules.mode === "free") {
      return `<div class="banner banner--info">${icon("globe")}<div>
        <strong>Mode Bebas: tidak ada situs yang diblokir.</strong>
        Daftar di bawah dipakai saat Praktikum dan Ujian.</div></div>`;
    }
    return "";
  }

  function siteCountText(rules) {
    return rules.mode === "free"
      ? "Disimpan; berlaku saat Praktikum dan Ujian."
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
        empty({ iconName: "alert", title: "Kode pemasangan tidak dapat dimuat", text: res.error }));
      return;
    }

    const open = res.open;
    const state = open
      ? `<span class="status status--online">${icon("dot", "status__icon")}Terbuka</span>`
      : `<span class="status status--offline">${icon("lock", "status__icon")}Ditutup</span>`;

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
            ? `${res.paired_count} PC sudah terpasang. Kode hanya dipakai sekali per PC.`
            : `Pendaftaran ditutup: PC baru ditolak meskipun kodenya benar. ${res.paired_count} PC yang sudah terpasang tetap terhubung.`}</div>
          <div class="pairing__actions">
            <button class="btn btn--sm" id="pair-rotate" ${open ? "" : "disabled"}>Buat kode baru</button>
            <button class="btn btn--sm" id="pair-toggle">${open ? "Tutup pendaftaran PC baru" : "Buka pendaftaran PC baru"}</button>
          </div>
        </div>
        <div class="pairing__steps">
          <div class="pairing__label">Cara memasang</div>
          <ol class="steps">
            <li>Pasang Lab Sentinel Agent di PC peserta.</li>
            <li>Jalankan sekali dengan kode ini:
              <code class="cmd">labsentinel-agent --pair ${esc(res.code)}</code></li>
            <li>PC muncul di <strong>Layar PC</strong> dalam beberapa detik. Kode tidak perlu diketik lagi.</li>
          </ol>
          <p class="pairing__hint">Setelah semua PC lab terpasang, tutup pendaftaran agar tidak ada PC lain yang bisa ikut terhubung.</p>
        </div>
      </div>`;

    $("#pair-rotate").addEventListener("click", async (e) => {
      const ok = await confirm({
        title: "Buat kode pemasangan baru?",
        message: "Kode lama langsung tidak berlaku. PC yang sudah terpasang tidak terpengaruh.",
        confirmLabel: "Buat kode baru",
      });
      if (!ok) return;
      await withBusy(e.currentTarget, async () => {
        const r = await API.rotatePairingCode();
        if (r.ok) { toast("Kode baru dibuat", groupCode(r.code), "ok"); renderPairing(); }
        else toast("Gagal", r.error, "error");
      });
    });

    $("#pair-toggle").addEventListener("click", async (e) => {
      if (open) {
        const ok = await confirm({
          title: "Tutup pendaftaran PC baru?",
          message: "PC yang belum terpasang tidak akan bisa terhubung sampai pendaftaran dibuka lagi. PC yang sudah terpasang tetap terhubung seperti biasa.",
          confirmLabel: "Tutup pendaftaran",
        });
        if (!ok) return;
      }
      await withBusy(e.currentTarget, async () => {
        const r = await API.setPairingOpen(!open);
        if (r.ok) {
          toast(r.open ? "Pendaftaran dibuka" : "Pendaftaran ditutup",
                r.open ? "PC baru bisa dipasang dengan kode." : "PC baru tidak bisa terhubung.", "ok");
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
      const live = s.status === "active" ? " · berjalan" : "";
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
        $(sel).title = none ? "Pilih satu sesi untuk membuat laporan." : "";
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
        toast("Laporan dibuat", r.saved_to ? `Salinan tersimpan di ${r.saved_to}` : r.filename, "ok");
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
        iconName: "check", title: "Belum ada pelanggaran tercatat",
        text: State.reportSession
          ? "Tidak ada pelanggaran pada sesi ini. Laporannya tetap bisa diunduh sebagai bukti sesi berjalan tertib."
          : "Riwayat akan terisi saat peserta membuka aplikasi atau situs yang tidak diizinkan.",
      }));
      return;
    }
    host.innerHTML = `
      <table class="table table--clickable">
        <thead><tr>
          <th style="width:130px">Waktu</th><th style="width:150px">PC</th>
          <th>Aplikasi / situs</th><th style="width:190px">Jenis</th>
          <th style="width:90px" class="cell--right">Bukti</th>
        </tr></thead>
        <tbody>${res.violations.map((v) => `
          <tr data-id="${v.id}">
            <td class="num">${esc(datetime(v.occurred_at))}</td>
            <td class="truncate">${esc(v.hostname || v.agent_id)}</td>
            <td class="truncate">${esc(v.subject)}
              ${v.dry_run ? '<span class="tag tag--muted">hanya dicatat</span>' : ""}</td>
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
        iconName: "history", title: "Belum ada aktivitas",
        text: "Setiap tindakan pengawas — menjeda, melanjutkan, mengganti aturan — akan tercatat di sini.",
      }));
      return;
    }
    host.innerHTML = `
      <table class="table">
        <thead><tr>
          <th style="width:150px">Waktu</th><th style="width:130px">Oleh</th>
          <th style="width:300px">Tindakan</th><th>Sasaran</th><th style="width:170px">Hasil</th>
        </tr></thead>
        <tbody>${res.entries.map((e) => `
          <tr>
            <td class="num">${esc(datetime(e.ts))}</td>
            <td>${esc(e.actor)}</td>
            <td title="${esc(e.action)}">${esc(actionLabel(e.action))}</td>
            <td class="truncate">${esc(e.target || "—")}</td>
            <td>${esc(e.result || "—")}</td>
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
