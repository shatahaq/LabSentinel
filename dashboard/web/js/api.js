/* Thin wrapper over the Eel bridge.
 *
 * Every call returns {ok, ...} or {ok:false, error}. The wrapper turns a
 * transport failure into the same shape, so UI code never has to distinguish
 * "the backend said no" from "the backend is gone" unless it wants to.
 */
(function (global) {
  "use strict";

  const available = typeof eel !== "undefined";

  async function call(name, ...args) {
    if (!available) {
      return { ok: false, error: "Tidak terhubung ke aplikasi dashboard." };
    }
    try {
      const result = await eel[name](...args)();
      if (result && typeof result === "object") return result;
      return { ok: false, error: "Balasan tidak dikenali dari dashboard." };
    } catch (err) {
      console.error("bridge call failed:", name, err);
      return { ok: false, error: "Dashboard tidak merespons. Coba lagi sebentar." };
    }
  }

  global.API = {
    available,
    pollEvents: (since) => call("poll_events", since || 0),
    getOverview: () => call("get_overview"),
    getAgents: () => call("get_agents"),
    getFrame: (id) => call("get_frame", id),
    setViewing: (gridOpen, focused) => call("set_viewing", gridOpen, focused || null),

    pause: (ids) => call("pause", ids || null),
    resume: (ids) => call("resume", ids || null),
    lockScreens: (ids) => call("lock_screens", ids || null),
    broadcast: (text, ids) => call("broadcast", text, ids || null),

    getRules: () => call("get_rules"),
    setMode: (mode) => call("set_mode", mode),
    setDryRun: (on) => call("set_dry_run", on),
    setWhitelist: (names) => call("set_whitelist", names),
    setSiteCategories: (keys) => call("set_site_categories", keys),
    setCloseGrace: (secs) => call("set_close_grace", secs),

    startSession: (name, mode) => call("start_session", name, mode),
    endSession: () => call("end_session"),
    listSessions: () => call("list_sessions"),

    getViolations: (sessionId, limit) => call("get_violations", sessionId || null, limit || 200),
    getScreenshot: (id) => call("get_screenshot", id),
    getAudit: (limit) => call("get_audit", limit || 300),
  };
})(window);
