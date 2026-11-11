// Buyer page: always shows the current drop, updated live over SSE, with PayPal buttons.
(() => {
  const { $, esc, api, toast, listen } = ND;
  const cfg = JSON.parse($("#config").textContent);
  let show = null;
  let drop = null;
  let renderedKey = null;
  let buttons = null;

  const ICONS = {
    check: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>`,
    x: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>`,
  };

  function view() {
    if (!show) return "loading";
    if (show.status !== "live") return "ended";
    if (!drop) return "waiting";
    return drop.state === "live" ? "live" : "sold_out";
  }

  function render(force = false) {
    const v = view();
    const key = `${v}:${drop ? drop.id : ""}`;
    if (!force && key === renderedKey) return updateNumbers();
    renderedKey = key;
    const stage = $("#stage");
    const pill = $("#show-pill");
    pill.className = show && show.status === "live" ? "pill pill-live" : "pill";
    pill.textContent = show && show.status === "live" ? "Live" : "Ended";
    if (buttons) { try { buttons.close(); } catch (_) { /* already gone */ } buttons = null; }

    if (v === "ended") {
      stage.innerHTML = `<div class="card waiting"><h2>The show has ended</h2><p class="muted">Thanks for watching! Follow the seller for the next live show.</p></div>`;
      return;
    }
    if (v === "waiting") {
      stage.innerHTML = `<div class="card waiting"><div class="radar"><span></span></div>
        <h2>Waiting for the next drop</h2>
        <p class="muted">Keep this page open. When the seller says the word, the product appears here — no need to reload.</p></div>`;
      return;
    }
    const attrs = Object.entries(drop.attributes || {})
      .map(([k, val]) => `<span class="attr">${esc(k)}: ${esc(val)}</span>`).join("");
    const img = drop.image_url
      ? `<div class="product-img" style="background-image:url('${esc(drop.image_url)}')"></div>`
      : `<div class="product-img">${esc(drop.title.charAt(0).toUpperCase())}</div>`;
    const soldOut = v === "sold_out";
    stage.innerHTML = `
      <article class="card product">
        ${img}
        <div class="product-body">
          <div class="row"><span class="pill ${soldOut ? "pill-sold" : "pill-live"}">${soldOut ? "Sold out" : "Live drop"}</span></div>
          <h1>${esc(drop.title)}</h1>
          ${drop.description ? `<p class="muted">${esc(drop.description)}</p>` : ""}
          ${attrs ? `<div class="attrs">${attrs}</div>` : ""}
          <div class="price-row">
            <div class="price">${esc(drop.price_display)}</div>
            <div class="units" id="units"><div class="stat-val" id="units-val">${drop.units_left}</div><div class="stat-lbl">${drop.units_left === 1 ? "unit left" : "units left"}</div></div>
          </div>
        </div>
        <div class="pay-zone">
          ${soldOut
            ? `<div class="banner" style="justify-content:center">Sold out. Stay here — the next drop appears automatically.</div>`
            : cfg.paypal ? `<div id="paypal-buttons"></div><p class="pay-note">Your payment is only taken if you get it. Too late? You're not charged.</p>`
              : `<div class="banner">Payments are not set up on this demo server yet.</div>`}
        </div>
      </article>`;
    if (!soldOut && cfg.paypal) mountButtons(drop.id);
  }

  function updateNumbers() {
    const el = $("#units-val");
    if (!el || !drop) return;
    if (el.textContent !== String(drop.units_left)) {
      el.textContent = drop.units_left;
      el.nextElementSibling.textContent = drop.units_left === 1 ? "unit left" : "units left";
      const box = $("#units");
      box.classList.remove("bump"); void box.offsetWidth; box.classList.add("bump");
    }
  }

  function whenPayPalReady(fn, tries = 0) {
    if (window.paypal && window.paypal.Buttons) return fn();
    if (tries > 100) {
      const zone = $("#paypal-buttons");
      if (zone) zone.innerHTML = `<div class="banner">PayPal could not load. Check your connection and reload.</div>`;
      return;
    }
    setTimeout(() => whenPayPalReady(fn, tries + 1), 100);
  }

  function mountButtons(dropId) {
    whenPayPalReady(() => {
      if (!$("#paypal-buttons") || !drop || drop.id !== dropId) return;
      let orderId = null;
      buttons = window.paypal.Buttons({
        style: { layout: "vertical", shape: "pill", label: "pay", height: 48 },
        createOrder: async () => {
          try {
            const res = await api(`/api/drops/${dropId}/orders`, { method: "POST" });
            orderId = res.id;
            return res.id;
          } catch (e) {
            showResult(e.data && e.data.result === "sold_out"
              ? { result: "lost", message: "Sold out — someone was faster. You were not charged." }
              : { result: "failed", message: e.message });
            throw e;
          }
        },
        onApprove: async (data) => {
          showProcessing();
          try {
            showResult(await api(`/api/orders/${data.orderID}/claim`, { method: "POST" }));
          } catch (e) {
            showResult({ result: "failed", message: `${e.message} You were not charged.` });
          }
        },
        onCancel: (data) => {
          const id = (data && data.orderID) || orderId;
          if (id) api(`/api/orders/${id}/abandon`, { method: "POST" }).catch(() => {});
        },
        onError: (err) => {
          console.error(err);
          if (orderId) api(`/api/orders/${orderId}/abandon`, { method: "POST" }).catch(() => {});
        },
      });
      buttons.render("#paypal-buttons").catch(() => {});
    });
  }

  // ------------------------------------------------------------ result overlay
  function openResult(cls, html) {
    const box = $("#result");
    box.className = `result ${cls}`;
    $("#result-card").innerHTML = html;
  }

  function showProcessing() {
    openResult("", `<div class="processing"><div class="spinner"></div><h2 id="result-title" style="font-size:1.4rem">Securing your payment…</h2>
      <p class="muted small">Checking if you were first.</p></div>`);
  }

  function showResult(r) {
    const close = `<button class="btn btn-block" id="result-close">Back to the show</button>`;
    if (r.result === "won") {
      openResult("won", `<div class="result-icon">${ICONS.check}</div>
        <h2 id="result-title">You got it!</h2>
        <p><strong>${esc(r.item)}</strong> · ${esc(r.amount_display)}</p>
        <span class="order-no">Order ${esc(r.order_number)}</span>
        <p class="muted small">Paid with PayPal. The seller will message you for shipping details.</p>${close}`);
    } else if (r.result === "lost") {
      openResult("lost", `<div class="result-icon">${ICONS.x}</div>
        <h2 id="result-title">Sold out</h2>
        <p>Someone was faster. <strong>You were not charged.</strong></p>
        <p class="muted small">Stay on this page — the next drop shows up here automatically.</p>${close}`);
    } else {
      openResult("failed", `<div class="result-icon">${ICONS.x}</div>
        <h2 id="result-title">Payment not completed</h2>
        <p>${esc(r.message || "Something went wrong.")}</p>${close}`);
    }
    $("#result-close").onclick = () => $("#result").classList.add("hidden");
    $("#result-close").focus();
  }

  // ------------------------------------------------------------ state + events
  async function load() {
    const st = await api(`/api/shows/${cfg.slug}/state`);
    show = st.show;
    drop = st.drop;
    render();
  }

  listen(`/api/shows/${cfg.slug}/events`, {
    drop_live: (d) => {
      const isNew = !drop || drop.id !== d.id;
      drop = d; render();
      if (isNew) toast("New drop is live!");
    },
    units_changed: (e) => {
      if (!drop || drop.id !== e.drop_id) return;
      drop = { ...drop, units_left: e.units_left, state: e.state };
      render();
      if (e.someone_bought) toast("Someone just got one!");
    },
    drop_sold_out: (d) => { if (drop && drop.id === d.id) { drop = d; render(); } },
    drop_closed: (d) => { if (drop && drop.id === d.id) { drop = null; render(); } },
    show_ended: () => { if (show) { show = { ...show, status: "ended" }; render(); } },
  }, () => load().catch(() => {}));

  load().catch(() => { $("#stage").innerHTML = `<div class="card waiting"><h2>Could not load the show</h2><p class="muted">Check your connection and reload.</p></div>`; });
})();
