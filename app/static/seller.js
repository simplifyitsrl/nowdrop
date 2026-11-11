// Seller console: voice/text -> drop, live drop card, order feed, assistant, PayPal activity.
(() => {
  const { $, esc, api, toast, listen, timeAgo, clock } = ND;
  const cfg = JSON.parse($("#config").textContent);
  const base = `/api/shows/${cfg.showId}`;
  const TRIGGER = { "en-US": "now", "es-419": "ahora" };
  let showStatus = cfg.status;
  let currentDrop = null;
  let photoFile = null;
  let dropInFlight = false;

  // ------------------------------------------------------------ drop card
  function renderDrop(drop) {
    currentDrop = drop;
    const pill = $("#drop-pill");
    const body = $("#drop-body");
    if (!drop) {
      pill.className = "pill"; pill.textContent = "None";
      body.innerHTML = `<p class="empty">No drop yet. Describe a product and say the trigger word.</p>`;
      return;
    }
    const labels = { live: ["pill pill-live", "Live"], sold_out: ["pill pill-sold", "Sold out"], closed: ["pill", "Closed"] };
    const [cls, text] = labels[drop.state] || labels.closed;
    pill.className = cls; pill.textContent = text;
    const attrs = Object.entries(drop.attributes || {})
      .map(([k, v]) => `<span class="attr">${esc(k)}: ${esc(v)}</span>`).join("");
    const thumb = drop.image_url
      ? `<div class="drop-thumb" style="background-image:url('${esc(drop.image_url)}')"></div>`
      : `<div class="drop-thumb">${esc(drop.title.charAt(0).toUpperCase())}</div>`;
    body.innerHTML = `
      <div class="drop-card">
        ${thumb}
        <div class="drop-meta">
          <h3>${esc(drop.title)}</h3>
          ${drop.description ? `<p class="small muted">${esc(drop.description)}</p>` : ""}
          ${attrs ? `<div class="attrs">${attrs}</div>` : ""}
          <div class="drop-numbers">
            <div><div class="stat-val">${esc(drop.price_display)}</div><div class="stat-lbl">Price</div></div>
            <div><div class="stat-val" id="units-left">${drop.units_left}/${drop.quantity}</div><div class="stat-lbl">Units left</div></div>
            <div><div class="stat-val" id="elapsed">${elapsedText(drop)}</div><div class="stat-lbl">${drop.sold_out_at ? "Sold out in" : "Elapsed"}</div></div>
          </div>
          ${drop.status === "live" ? `<div><button class="btn btn-ghost btn-sm" id="close-drop">Close drop</button></div>` : ""}
        </div>
      </div>`;
    const btn = $("#close-drop");
    if (btn) btn.onclick = async () => {
      btn.disabled = true;
      try { await api(`${base}/drops/${drop.id}/close`, { method: "POST" }); }
      catch (e) { toast(e.message); btn.disabled = false; }
    };
  }

  function elapsedText(drop) {
    if (drop.sold_out_at) {
      const s = (new Date(drop.sold_out_at) - new Date(drop.created_at)) / 1000;
      return `${s.toFixed(1)}s`;
    }
    return drop.status === "live" ? timeAgo(drop.created_at) : "—";
  }

  setInterval(() => {
    const el = $("#elapsed");
    if (el && currentDrop) el.textContent = elapsedText(currentDrop);
  }, 1000);

  // ------------------------------------------------------------ orders feed
  const orderEls = new Map();

  function orderHtml(o) {
    const refunded = o.status === "refunded";
    return `
      <div><div class="who">${esc(o.buyer_name)}</div>
        <div class="tiny muted">${esc(o.order_number)} · ${esc(o.item)} · ${clock(o.created_at)}</div></div>
      <div class="amt">${esc(o.amount_display)}</div>
      <div class="tiny muted">${esc(o.buyer_email || "")}</div>
      <div style="text-align:right">${refunded ? `<span class="pill">Refunded</span>`
        : `<button class="btn btn-ghost btn-sm" data-refund="${o.id}">Refund</button>`}</div>`;
  }

  function upsertOrder(o, isNew = false) {
    $("#feed-empty")?.remove();
    let el = orderEls.get(o.id);
    if (!el) {
      el = document.createElement("div");
      orderEls.set(o.id, el);
      $("#feed").prepend(el);
    }
    el.className = `feed-item${o.status === "refunded" ? " refunded" : ""}${isNew ? " new" : ""}`;
    el.innerHTML = orderHtml(o);
  }

  function addLost(ev) {
    $("#feed-empty")?.remove();
    const el = document.createElement("div");
    el.className = "feed-item lost new";
    el.innerHTML = `<div class="small">${esc(ev.payer_name)} was too late for ${esc(ev.item)}</div><div class="tiny">voided · not charged</div>`;
    $("#feed").prepend(el);
  }

  async function refundOrder(orderId, button) {
    if (!confirm("Refund this order in full through PayPal?")) return false;
    if (button) button.disabled = true;
    try {
      const { order } = await api(`/api/orders/${orderId}/refund`, { method: "POST", json: { show_id: cfg.showId } });
      upsertOrder(order);
      toast(`Refunded ${order.amount_display} to ${order.buyer_name}`);
      refreshStats();
      return true;
    } catch (e) {
      toast(e.message, 4000);
      if (button) button.disabled = false;
      return false;
    }
  }

  $("#feed").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-refund]");
    if (btn) refundOrder(Number(btn.dataset.refund), btn);
  });

  function renderStats(stats) {
    $("#kpi-revenue").textContent = stats.revenue_display;
    $("#kpi-units").textContent = stats.units_sold;
    $("#kpi-late").textContent = stats.buyers_too_late;
  }

  async function refreshStats() {
    try { renderStats((await api(`/api/seller/${cfg.showId}/state`)).stats); } catch (_) { /* ignore */ }
  }

  // ------------------------------------------------------------ PayPal log
  const PP_NAMES = [
    [/oauth2\/token$/, "OAuth token"], [/\/checkout\/orders$/, "Create order"],
    [/\/authorize$/, "Authorize"], [/\/capture$/, "Capture"], [/\/void$/, "Void"], [/\/refund$/, "Refund"],
  ];
  let ppCount = 0;
  function ppName(path) {
    const hit = PP_NAMES.find(([re]) => re.test(path));
    return hit ? hit[1] : path;
  }
  function addLog(entry, isNew = false) {
    const ok = entry.status_code >= 200 && entry.status_code < 300;
    const el = document.createElement("div");
    if (isNew) el.className = "new";
    el.title = `${entry.method} ${entry.path}\nPayPal-Debug-Id: ${entry.debug_id || "—"}`;
    el.innerHTML = `<span class="${ok ? "code-ok" : "code-bad"}">${entry.status_code || "ERR"}</span>
      <span class="path"><strong>${esc(ppName(entry.path))}</strong> ${esc(entry.method)} ${esc(entry.path)}</span>
      <span class="muted">${clock(entry.created_at)}</span>`;
    $("#pplog").prepend(el);
    ppCount += 1;
    $("#pp-count").textContent = `${ppCount} call${ppCount === 1 ? "" : "s"}`;
  }

  // ------------------------------------------------------------ state + events
  async function loadState() {
    const st = await api(`/api/seller/${cfg.showId}/state`);
    renderDrop(st.drop);
    orderEls.clear();
    $("#feed").innerHTML = st.orders.length ? "" : `<p class="empty" id="feed-empty">Winners appear here the moment they pay.</p>`;
    [...st.orders].reverse().forEach((o) => upsertOrder(o));
    $("#pplog").innerHTML = ""; ppCount = 0;
    [...st.paypal_log].reverse().forEach((e) => addLog(e));
    renderStats(st.stats);
    setShowStatus(st.show.status);
  }

  function setShowStatus(status) {
    showStatus = status;
    const pill = $("#show-status");
    pill.className = status === "live" ? "pill pill-live" : "pill";
    pill.textContent = status === "live" ? "Live" : "Ended";
    $("#end-show").disabled = status !== "live";
    $("#drop-btn").disabled = status !== "live";
  }

  const sameDrop = (d) => currentDrop && d && currentDrop.id === d.drop_id;
  listen(`/api/seller/${cfg.showId}/events`, {
    drop_live: (d) => renderDrop(d),
    drop_closed: (d) => { if (!currentDrop || currentDrop.id === d.id) renderDrop(d); },
    drop_sold_out: (d) => { renderDrop(d); toast(`Sold out: ${d.title}`); },
    units_changed: (e) => {
      if (!sameDrop(e)) return;
      currentDrop = { ...currentDrop, units_left: e.units_left, state: e.state };
      const el = $("#units-left");
      if (el) el.textContent = `${e.units_left}/${currentDrop.quantity}`;
    },
    order_won: (o) => { upsertOrder(o, true); toast(`${o.buyer_name} got ${o.item}!`); refreshStats(); },
    order_refunded: (o) => { upsertOrder(o); refreshStats(); },
    attempt_lost: (e) => { addLost(e); refreshStats(); },
    paypal_log: (e) => addLog(e, true),
    show_ended: () => setShowStatus("ended"),
  }, () => loadState().catch(() => {}));

  // ------------------------------------------------------------ creating drops
  function setStatus(kind, html) {
    const el = $("#ai-status");
    el.className = `ai-status ${kind}`;
    el.innerHTML = html;
  }

  async function submitDrop(text, source) {
    if (showStatus !== "live") { toast("This show has ended."); return false; }
    if (!text.trim()) { toast("Describe the product first."); return false; }
    dropInFlight = true;
    const started = performance.now();
    setStatus("working", `<span class="spinner" style="width:14px;height:14px;border-width:2px;display:inline-block;vertical-align:-2px"></span>&nbsp; Reading ${source === "voice" ? "what you said" : "your description"}…`);
    const form = new FormData();
    form.append("text", text);
    if (photoFile) form.append("photo", photoFile);
    try {
      const { drop } = await api(`${base}/drops/from-speech`, { method: "POST", form });
      const secs = ((performance.now() - started) / 1000).toFixed(1);
      setStatus("ok", `✓ <strong>${esc(drop.title)}</strong> is live at ${esc(drop.price_display)} · ${drop.quantity} unit${drop.quantity === 1 ? "" : "s"} · ${secs}s`);
      clearPhoto();
      renderDrop(drop);
      return true;
    } catch (e) {
      const missing = (e.data && e.data.missing) || [];
      const hint = missing.includes("price")
        ? (source === "voice" ? " Just say the price, then the trigger word again." : " Add the price and press Drop it again.")
        : "";
      setStatus("error", `${esc(e.message)}${hint}`);
      return false;
    } finally {
      dropInFlight = false;
    }
  }

  $("#text-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const box = $("#drop-text");
    if (dropInFlight) return;
    $("#drop-btn").disabled = true;
    if (await submitDrop(box.value, "text")) box.value = "";
    $("#drop-btn").disabled = showStatus !== "live";
  });
  $("#drop-text").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) $("#text-form").requestSubmit();
  });

  $("#manual-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    try {
      const { drop } = await api(`${base}/drops`, { method: "POST", json: {
        title: f.get("title"), price: Number(f.get("price")), quantity: Number(f.get("quantity") || 1),
        description: f.get("description") || "",
      } });
      renderDrop(drop);
      ev.target.reset();
      toast("Drop is live");
    } catch (e) { toast(e.message, 4000); }
  });

  // Photo for the next drop
  function clearPhoto() {
    photoFile = null;
    $("#photo").value = "";
    $("#photo-chip").classList.add("hidden");
  }
  $("#photo").addEventListener("change", (ev) => {
    const file = ev.target.files[0];
    if (!file) return clearPhoto();
    if (file.size > 5 * 1024 * 1024) { toast("Photo is too large (max 5 MB)"); return clearPhoto(); }
    photoFile = file;
    $("#photo-preview").src = URL.createObjectURL(file);
    $("#photo-chip").classList.remove("hidden");
  });
  $("#photo-clear").addEventListener("click", clearPhoto);

  // ------------------------------------------------------------ voice
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const words = cfg.triggerWords.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const triggerRe = new RegExp(`(^|[^\\p{L}])(${words.join("|")})(?=$|[^\\p{L}])`, "giu");
  let recognition = null;
  let listening = false;
  let buffer = "";     // final text since the last drop
  let interim = "";

  function renderTranscript() {
    const el = $("#transcript");
    if (!buffer && !interim) {
      el.innerHTML = `<span class="muted">${listening ? "Listening…" : "Live transcript appears here…"}</span>`;
      return;
    }
    const mark = (t) => esc(t).replace(triggerRe, (_m, pre, w) => `${pre}<mark>${w}</mark>`);
    el.innerHTML = `${mark(buffer)} <span class="interim">${mark(interim)}</span>`;
    el.scrollTop = el.scrollHeight;
  }

  function handleFinal(segment) {
    buffer = `${buffer} ${segment}`.trim();
    triggerRe.lastIndex = 0;
    let last = null, m;
    while ((m = triggerRe.exec(buffer)) !== null) last = m;
    if (!last) return;
    const cut = last.index + last[0].length;
    const toSend = buffer.slice(0, cut).trim();
    buffer = buffer.slice(cut).trim();
    submitDrop(toSend, "voice").then((ok) => {
      // Missing price: keep the description so the seller only has to add the price.
      if (!ok) buffer = `${toSend.replace(triggerRe, "$1").trim()} ${buffer}`.trim();
      renderTranscript();
    });
  }

  function setListening(on) {
    listening = on;
    const mic = $("#mic");
    mic.classList.toggle("on", on);
    mic.setAttribute("aria-pressed", String(on));
    mic.setAttribute("aria-label", on ? "Stop listening" : "Start listening");
    $("#mic-label").textContent = on ? `Listening — say “${TRIGGER[$("#lang").value]}” to drop` : "Tap to start listening";
    renderTranscript();
  }

  function startRecognition() {
    recognition = new Recognition();
    recognition.lang = $("#lang").value;
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.onresult = (ev) => {
      interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const text = ev.results[i][0].transcript;
        if (ev.results[i].isFinal) handleFinal(text);
        else interim += text;
      }
      renderTranscript();
    };
    recognition.onerror = (ev) => {
      if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
        toast("Microphone access was blocked. Allow it in the browser, or type instead.", 4500);
        setListening(false);
      }
    };
    // Chrome stops after silence; keep going while the seller wants to listen.
    recognition.onend = () => { if (listening) { try { recognition.start(); } catch (_) { /* already started */ } } };
    recognition.start();
  }

  if (!Recognition) {
    $("#speech-unsupported").classList.remove("hidden");
    $("#mic").disabled = true;
    $("#mic-label").textContent = "Voice needs Chrome — type below";
  } else {
    $("#mic").addEventListener("click", () => {
      if (listening) {
        setListening(false);
        recognition && recognition.stop();
      } else {
        setListening(true);
        try { startRecognition(); } catch (e) { toast("Could not start the microphone"); setListening(false); }
      }
    });
  }

  function syncTriggerHint() {
    $("#trigger-word").textContent = TRIGGER[$("#lang").value] || cfg.triggerWords[0];
    if (listening) setListening(true);
  }
  $("#lang").addEventListener("change", async () => {
    syncTriggerHint();
    api(`${base}/language`, { method: "POST", json: { language: $("#lang").value } }).catch(() => {});
    if (listening && recognition) { recognition.onend = null; recognition.stop(); startRecognition(); }
  });
  syncTriggerHint();

  // ------------------------------------------------------------ assistant
  const history = [];
  function addMsg(role, html) {
    const el = document.createElement("div");
    el.className = `msg ${role === "user" ? "user" : "bot"}`;
    el.innerHTML = html;
    $("#chat-log").append(el);
    $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
    return el;
  }

  async function ask(question) {
    if (!question.trim()) return;
    history.push({ role: "user", content: question });
    addMsg("user", esc(question));
    const pending = addMsg("bot", `<span class="muted">Thinking…</span>`);
    try {
      const res = await api(`${base}/assistant`, { method: "POST", json: { messages: history } });
      history.push({ role: "assistant", content: res.reply });
      pending.innerHTML = esc(res.reply);
      for (const action of res.actions || []) {
        if (action.type !== "refund") continue;
        const o = action.order;
        const box = document.createElement("div");
        box.className = "confirm";
        box.innerHTML = `<button class="btn btn-accent btn-sm">Confirm refund ${esc(o.amount_display)} to ${esc(o.buyer_name)}</button>
          <button class="btn btn-ghost btn-sm">Cancel</button>`;
        const [yes, no] = box.querySelectorAll("button");
        yes.onclick = async () => {
          if (await refundOrder(o.id, yes)) box.innerHTML = `<span class="pill pill-live">Refunded ${esc(o.order_number)}</span>`;
        };
        no.onclick = () => { box.innerHTML = `<span class="pill">Refund cancelled</span>`; };
        pending.append(box);
      }
    } catch (e) {
      history.pop();
      pending.innerHTML = `<span style="color:var(--warn)">${esc(e.message)}</span>`;
    }
  }

  $("#chat-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = $("#chat-input");
    const q = input.value;
    input.value = "";
    ask(q);
  });
  document.querySelectorAll("[data-q]").forEach((b) => b.addEventListener("click", () => ask(b.dataset.q)));

  // ------------------------------------------------------------ misc
  $("#copy-link").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#show-url").value); toast("Link copied"); }
    catch (_) { $("#show-url").select(); }
  });

  $("#end-show").addEventListener("click", async () => {
    if (!confirm("End the show? The current drop closes and buyers see that the show is over.")) return;
    try {
      const { recap_url } = await api(`${base}/end`, { method: "POST" });
      if (listening) { setListening(false); recognition && recognition.stop(); }
      location.href = recap_url;
    } catch (e) { toast(e.message); }
  });
})();
