// Small helpers shared by the seller console and the buyer page.
const ND = (() => {
  const $ = (sel, root = document) => root.querySelector(sel);

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

  async function api(path, { method = "GET", json, form } = {}) {
    const opts = { method, headers: {} };
    if (json !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(json);
    } else if (form) {
      opts.body = form;
    }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) {
      const err = new Error((data && (data.error || data.detail)) || `Request failed (${res.status})`);
      err.status = res.status;
      err.data = data || {};
      if (Array.isArray(err.data.detail)) err.message = "Please check the form values.";
      throw err;
    }
    return data;
  }

  let toastTimer;
  function toast(text, ms = 2600) {
    const el = $("#toast");
    if (!el) return;
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), ms);
  }

  // EventSource reconnects by itself; `onOpen` lets pages resync state after a gap.
  function listen(url, handlers, onOpen) {
    const source = new EventSource(url);
    source.onopen = () => onOpen && onOpen();
    for (const [name, fn] of Object.entries(handlers)) {
      source.addEventListener(name, (ev) => {
        try { fn(JSON.parse(ev.data)); } catch (e) { console.error(name, e); }
      });
    }
    return source;
  }

  function timeAgo(iso) {
    const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
  }

  const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  return { $, esc, api, toast, listen, timeAgo, clock };
})();
