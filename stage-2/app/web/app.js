(() => {
  "use strict";

  const root = document.getElementById("app");
  const sessionKey = "tablekeeper-session";
  const readSession = () => {
    try { return JSON.parse(localStorage.getItem(sessionKey) || "null"); }
    catch { return null; }
  };
  let session = readSession();
  let restaurants = [];
  let activeSearch = null;
  let searchNumber = 0;
  let selection = null;
  let pendingBooking = null;
  let lastConfirmation = null;
  let searchContext = null;

  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
  const timeLabel = (local) => local.slice(11, 16);
  const localDate = () => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  };
  const tokenHeaders = () => session?.token ? { Authorization: `Bearer ${session.token}` } : {};
  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    Object.assign(headers, tokenHeaders());
    const response = await fetch(path, { ...options, headers });
    let payload = null;
    try { payload = response.status === 204 ? null : await response.json(); }
    catch { payload = null; }
    return { response, payload };
  }
  const apiError = (result) => result.payload?.error?.message || result.payload?.error?.code || "The request could not be completed.";
  const currentRoute = () => ["/signup", "/login", "/lookup"].includes(location.pathname) ? location.pathname : "/";
  const navLink = (href, label, route) => `<a href="${href}" ${route === currentRoute() ? 'aria-current="page"' : ""}>${label}</a>`;
  function shell(content) {
    const user = session ? `<span class="session-name" data-testid="current-user">Signed in as ${escapeHtml(session.display_name)}</span><button class="quiet-button" data-testid="logout-button" type="button">Log out</button>` : "";
    root.innerHTML = `
      <header class="site-header">
        <div class="header-inner">
          <a class="brand" href="/" aria-label="Tablekeeper home"><span class="brand-mark" aria-hidden="true">T</span>Tablekeeper</a>
          <nav class="main-nav" aria-label="Main navigation">
            ${navLink("/", "Find a table", "/")}
            ${navLink("/lookup", "My reservation", "/lookup")}
            ${session ? "" : `${navLink("/login", "Log in", "/login")}${navLink("/signup", "Create account", "/signup")}`}
          </nav>
          ${user ? `<div class="session">${user}</div>` : ""}
        </div>
      </header>
      <main>${content}</main>
      <footer class="footer"><div class="footer-inner">Thoughtful tables for evenings worth sharing.</div></footer>`;
    root.querySelector('[data-testid="logout-button"]')?.addEventListener("click", () => {
      localStorage.removeItem(sessionKey);
      session = null;
      selection = null;
      pendingBooking = null;
      lastConfirmation = null;
      render();
    });
  }

  async function getRestaurants() {
    const { response, payload } = await api("/restaurants");
    if (!response.ok || !Array.isArray(payload?.restaurants)) throw new Error(apiError({ payload }));
    restaurants = payload.restaurants;
    return restaurants;
  }
  async function getRestaurant(id) {
    const { response, payload } = await api(`/restaurants/${encodeURIComponent(id)}`);
    if (!response.ok) throw new Error(apiError({ payload }));
    return payload;
  }
  const labelFor = (config, id) => config.tables.find((table) => table.id === id)?.label || id;
  const labelsFor = (config, ids) => ids.map((id) => `Table ${labelFor(config, id)}`).join(" + ");
  function feedback(id, text, type) {
    return text ? `<p class="feedback ${type}" data-testid="${id}" role="${type === "error" ? "alert" : "status"}">${escapeHtml(text)}</p>` : "";
  }

  async function renderSearch() {
    shell(`
      <section class="hero">
        <p class="eyebrow">A good evening starts here</p>
        <h1>Make room for<br>something memorable.</h1>
        <p class="intro">Find a table at a neighborhood favorite. Choose a time, bring your people, and we’ll take care of the rest.</p>
      </section>
      <section class="panel search-panel" aria-labelledby="search-heading">
        <h2 id="search-heading">Find your table</h2>
        <form id="search-form" class="search-fields">
          <div class="field"><label for="restaurant-select">Restaurant</label><select id="restaurant-select" data-testid="restaurant-select" required><option value="">Choose a restaurant</option></select></div>
          <div class="field"><label for="date-input">Date</label><input id="date-input" data-testid="date-input" type="date" required value="${localDate()}"></div>
          <div class="field"><label for="party-size-input">Party size</label><input id="party-size-input" data-testid="party-size-input" type="number" min="1" step="1" value="2" required></div>
          <button class="button" data-testid="search-button" type="submit">Search availability</button>
        </form>
        <div id="search-feedback" aria-live="polite"></div>
      </section>
      <section aria-labelledby="availability-heading">
        <div class="section-heading"><h2 id="availability-heading">Available times</h2><p class="section-note">Choose a seat that feels right.</p></div>
        <div id="search-results"></div>
      </section>
      <section id="booking-area" aria-live="polite"></section>`);
    const restaurantSelect = root.querySelector("#restaurant-select");
    let list = [];
    try {
      list = await getRestaurants();
      restaurantSelect.innerHTML = `<option value="">Choose a restaurant</option>${list.map((item) =>
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("")}`;
      if (list.length === 1) restaurantSelect.value = list[0].id;
    } catch (error) {
      root.querySelector("#search-feedback").innerHTML = feedback("search-error", error.message, "error");
    }
    root.querySelector("#search-form").addEventListener("submit", (event) => {
      event.preventDefault();
      runSearch();
    });
    root.querySelector("#booking-area").addEventListener("submit", submitBooking);
    root.querySelector("#search-results").addEventListener("click", (event) => {
      const button = event.target.closest("[data-option]");
      if (!button || button.dataset.available !== "true") return;
      if (!session) {
        root.querySelector("#search-feedback").innerHTML = feedback("auth-error", "Please log in to reserve a table.", "error");
        return;
      }
      const slot = activeSearch.result.slots.find((item) => item.starts_at_local === button.dataset.starts);
      if (!slot) return;
      selection = {
        restaurantId: activeSearch.context.restaurantId,
        config: activeSearch.config,
        start: slot.starts_at_local,
        tableIds: button.dataset.ids.split("|"),
        partySize: activeSearch.context.partySize
      };
      pendingBooking = null;
      lastConfirmation = null;
      root.querySelector("#search-feedback").innerHTML = "";
      renderBooking();
      root.querySelector("#booking-form")?.scrollIntoView({ behavior: "smooth", block: "center" });
      root.querySelector("#booking-party-size")?.focus({ preventScroll: true });
    });
    if (list?.length === 1) runSearch();
  }

  function readSearchContext() {
    return {
      restaurantId: root.querySelector("#restaurant-select").value,
      date: root.querySelector("#date-input").value,
      partySize: Number(root.querySelector("#party-size-input").value)
    };
  }
  function sameContext(a, b) {
    return a && b && a.restaurantId === b.restaurantId && a.date === b.date && a.partySize === b.partySize;
  }
  async function runSearch(options = {}) {
    const context = options.context || readSearchContext();
    if (!context.restaurantId || !context.date || !Number.isInteger(context.partySize) || context.partySize < 1) {
      root.querySelector("#search-feedback").innerHTML = feedback("search-error", "Choose a restaurant, date, and party size to search.", "error");
      return;
    }
    if (!options.preserveSelection) {
      selection = null;
      pendingBooking = null;
      lastConfirmation = null;
      root.querySelector("#booking-area").innerHTML = "";
    }
    searchContext = context;
    const requestNumber = ++searchNumber;
    const resultBox = root.querySelector("#search-results");
    root.querySelector("#search-feedback").innerHTML = "";
    resultBox.setAttribute("aria-busy", "true");
    resultBox.innerHTML = '<p class="loading" role="status" data-testid="search-loading">Finding a little room for you…</p>';
    try {
      const query = new URLSearchParams({ restaurant_id: context.restaurantId, date: context.date, party_size: String(context.partySize) });
      const [availability, config] = await Promise.all([
        api(`/availability?${query}`),
        getRestaurant(context.restaurantId)
      ]);
      if (requestNumber !== searchNumber || !sameContext(context, readSearchContext())) return;
      if (!availability.response.ok) throw new Error(apiError(availability));
      activeSearch = { context, result: availability.payload, config };
      renderGrid(activeSearch);
      if (options.preserveSelection) renderBooking();
    } catch (error) {
      if (requestNumber !== searchNumber || !sameContext(context, readSearchContext())) return;
      resultBox.innerHTML = feedback("search-error", error.message || "Availability could not be loaded. Please try again.", "error");
    } finally {
      if (requestNumber === searchNumber) {
        resultBox.setAttribute("aria-busy", "false");
      }
    }
  }
  function renderGrid(search) {
    const box = root.querySelector("#search-results");
    if (!search.result.slots.length) {
      box.innerHTML = '<div class="empty-state" data-testid="no-slots">No seating times are listed for this day. Try another date.</div>';
      return;
    }
    const tables = search.config.tables;
    box.innerHTML = `<div class="slot-list" data-testid="availability-grid">${search.result.slots.map((slot) => {
      const singles = tables.map((table) => {
        const available = slot.available_table_ids.includes(table.id);
        const label = `Table ${table.label}`;
        return seatButton([table.id], label, slot.starts_at_local, available, table.capacity);
      }).join("");
      const pairs = (slot.available_options || []).filter((option) => option.table_ids.length === 2)
        .map((option) => seatButton(option.table_ids, labelsFor(search.config, option.table_ids),
          slot.starts_at_local, true, option.capacity)).join("");
      return `<article class="panel slot-row"><time class="slot-time" datetime="${escapeHtml(slot.starts_at)}">${escapeHtml(timeLabel(slot.starts_at_local))}</time><div class="seat-list">${singles}${pairs}</div></article>`;
    }).join("")}</div>`;
  }
  function seatButton(ids, label, starts, available, capacity) {
    const pairKey = ids.length === 2 ? `${ids[0]}+${ids[1]}` : ids[0];
    const testId = `slot-${pairKey}-${timeLabel(starts)}`;
    const text = ids.length === 2 ? `${label.replace(" + ", " & ")} together` : label;
    return `<button type="button" class="seat-choice ${available ? "" : "unavailable"}" data-testid="${escapeHtml(testId)}" data-option="${escapeHtml(pairKey)}" data-ids="${escapeHtml(ids.join("|"))}" data-starts="${escapeHtml(starts)}" data-available="${available}" ${available ? "" : "aria-disabled=\"true\""}><span class="seat-label">${escapeHtml(text)}</span><span class="seat-meta">${available ? `Seats up to ${capacity}` : "Unavailable"}</span></button>`;
  }
  function renderBooking() {
    if (!selection) return;
    const { config, tableIds, start, partySize } = selection;
    const summary = `${config.name} · ${labelsFor(config, tableIds)} · ${start.replace("T", " at ")} local time`;
    const confirmation = lastConfirmation ? renderConfirmation(lastConfirmation) : "";
    root.querySelector("#booking-area").innerHTML = `
      <section class="panel booking-panel">
        <p class="eyebrow">Your evening</p><h2>Reserve this table</h2>
        <p class="booking-summary" data-testid="booking-summary">${escapeHtml(summary)}</p>
        <form data-testid="booking-form" id="booking-form">
          <div class="booking-fields">
            <div class="field"><label for="booking-party-size">Party size</label><input id="booking-party-size" data-testid="booking-party-size" type="number" min="1" step="1" required value="${partySize}"></div>
            <button class="button" data-testid="booking-submit" type="submit">Confirm reservation</button>
          </div>
          <div id="booking-feedback">${lastConfirmation ? "" : ""}</div>
          ${pendingBooking?.uncertain ? feedback("booking-uncertain", "We couldn’t confirm whether the reservation was received. Your request is saved; retry without changing it to check the original booking.", "notice") : ""}
          ${confirmation}
        </form>
      </section>`;
    root.querySelector("#booking-party-size").addEventListener("input", () => {
      pendingBooking = null;
      lastConfirmation = null;
      root.querySelector("#booking-feedback").innerHTML = "";
      root.querySelector('[data-testid="booking-uncertain"]')?.remove();
      root.querySelector('[data-testid="confirmation"]')?.remove();
    });
  }
  function renderConfirmation(record) {
    const tables = record.table_ids || [record.table_id];
    return `<section class="confirmation" data-testid="confirmation" aria-live="polite">
      <p class="eyebrow">Your table is waiting</p><h3>Reservation confirmed</h3>
      <p>Reference</p><div class="confirmation-reference" data-testid="confirmation-reference">${escapeHtml(record.reference)}</div>
      <p data-testid="confirmation-tables">${escapeHtml(labelsFor(selection.config, tables))}</p>
      <p data-testid="confirmation-details">${escapeHtml(`${selection.config.name} · ${labelsFor(selection.config, tables)} · ${record.starts_at_local.replace("T", " at ")} local time`)}</p>
    </section>`;
  }
  function bookingBody() {
    const size = Number(root.querySelector("#booking-party-size").value);
    return {
      restaurant_id: selection.restaurantId,
      ...(selection.tableIds.length === 1 ? { table_id: selection.tableIds[0] } : { table_ids: [...selection.tableIds] }),
      starts_at_local: selection.start,
      party_size: size
    };
  }
  async function submitBooking(event) {
    event.preventDefault();
    if (!selection || !session) return;
    const body = bookingBody();
    if (!Number.isInteger(body.party_size) || body.party_size < 1) {
      root.querySelector("#booking-feedback").innerHTML = feedback("booking-error", "Party size must be at least one.", "error");
      return;
    }
    const serialized = JSON.stringify(body);
    if (!pendingBooking || pendingBooking.serialized !== serialized) {
      pendingBooking = { body, serialized, key: crypto.randomUUID(), uncertain: false };
      lastConfirmation = null;
    }
    const pending = pendingBooking;
    const button = root.querySelector('[data-testid="booking-submit"]');
    button.disabled = true;
    root.querySelector("#booking-feedback").innerHTML = "";
    root.querySelector('[data-testid="confirmation"]')?.remove();
    try {
      const result = await api("/reservations", {
        method: "POST",
        headers: { "Idempotency-Key": pending.key },
        body: JSON.stringify(pending.body)
      });
      if (result.response.status === 201 || result.response.status === 200) {
        if (!result.payload || typeof result.payload.reference !== "string") throw new Error("The response was incomplete.");
        pending.uncertain = false;
        lastConfirmation = result.payload;
        renderBooking();
        return;
      }
      pending.uncertain = false;
      root.querySelector('[data-testid="booking-uncertain"]')?.remove();
      const message = apiError(result);
      if (result.response.status === 409 && result.payload?.error?.code === "table_unavailable") {
        root.querySelector("#booking-feedback").innerHTML = feedback("booking-error", "That table was just taken. Your choices are still here; please select another time or table.", "error");
        const context = searchContext;
        if (context) runSearch({ context, preserveSelection: true });
      } else {
        root.querySelector("#booking-feedback").innerHTML = feedback("booking-error", message, "error");
      }
    } catch {
      pending.uncertain = true;
      lastConfirmation = null;
      root.querySelector("#booking-feedback").innerHTML = "";
      root.querySelector('[data-testid="confirmation"]')?.remove();
      root.querySelector('[data-testid="booking-uncertain"]')?.remove();
      root.querySelector("#booking-form").insertAdjacentHTML("beforeend",
        feedback("booking-uncertain", "We couldn’t confirm whether the reservation was received. Your request is saved; retry without changing it to check the original booking.", "notice"));
    } finally {
      root.querySelector('[data-testid="booking-submit"]')?.removeAttribute("disabled");
    }
  }

  async function renderAuth(signup) {
    shell(`<section class="panel auth-card">
      <p class="eyebrow">${signup ? "A seat at the table" : "Welcome back"}</p>
      <h1>${signup ? "Save your place." : "Good to see you."}</h1>
      <p class="intro">${signup ? "Create an account to book and look after your reservations." : "Log in to book a table and manage your evening."}</p>
      <form id="auth-form">
        ${signup ? `<div class="field"><label for="signup-display-name">Name</label><input id="signup-display-name" data-testid="signup-display-name" autocomplete="name" required></div>` : ""}
        <div class="field"><label for="${signup ? "signup-email" : "login-email"}">Email</label><input id="${signup ? "signup-email" : "login-email"}" data-testid="${signup ? "signup-email" : "login-email"}" type="email" autocomplete="email" required></div>
        <div class="field"><label for="${signup ? "signup-password" : "login-password"}">Password</label><input id="${signup ? "signup-password" : "login-password"}" data-testid="${signup ? "signup-password" : "login-password"}" type="password" minlength="${signup ? 8 : 1}" autocomplete="${signup ? "new-password" : "current-password"}" required></div>
        <button class="button" data-testid="${signup ? "signup-submit" : "login-submit"}" type="submit">${signup ? "Create account" : "Log in"}</button>
        <div id="auth-feedback" aria-live="polite"></div>
      </form>
      <p class="card-note">${signup ? `Already have an account? <a href="/login">Log in</a>` : `New to Tablekeeper? <a href="/signup">Create an account</a>`}</p>
    </section>`);
    root.querySelector("#auth-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.currentTarget.querySelector("button");
      button.disabled = true;
      const body = {
        email: root.querySelector(`[data-testid="${signup ? "signup-email" : "login-email"}"]`).value,
        password: root.querySelector(`[data-testid="${signup ? "signup-password" : "login-password"}"]`).value
      };
      if (signup) body.display_name = root.querySelector('[data-testid="signup-display-name"]').value;
      try {
        const result = await api(`/auth/${signup ? "signup" : "login"}`, { method: "POST", body: JSON.stringify(body) });
        if (!result.response.ok) throw new Error(apiError(result));
        session = { user_id: result.payload.user_id, display_name: result.payload.display_name, token: result.payload.token };
        localStorage.setItem(sessionKey, JSON.stringify(session));
        history.pushState({}, "", "/");
        render();
      } catch (error) {
        root.querySelector("#auth-feedback").innerHTML = feedback("auth-error", error.message || "Your account could not be verified.", "error");
      } finally {
        root.querySelector("#auth-form button")?.removeAttribute("disabled");
      }
    });
  }

  async function renderLookup() {
    shell(`<section class="panel lookup-card">
      <p class="eyebrow">Keep your plans close</p><h1>Look up a reservation.</h1>
      <p class="intro">Enter the reference from your confirmation to see your booking or cancel it.</p>
      <form id="lookup-form">
        <div class="field"><label for="lookup-reference-input">Reservation reference</label><input id="lookup-reference-input" data-testid="lookup-reference-input" autocomplete="off" required></div>
        <button class="button" data-testid="lookup-submit" type="submit">Find reservation</button>
      </form>
      <div id="lookup-feedback" aria-live="polite"></div><div id="reservation-result"></div>
    </section>`);
    root.querySelector("#lookup-form").addEventListener("submit", (event) => {
      event.preventDefault();
      lookup(root.querySelector('[data-testid="lookup-reference-input"]').value.trim());
    });
    root.querySelector("#reservation-result").addEventListener("click", (event) => {
      if (event.target.closest('[data-testid="reservation-cancel-button"]')) cancelReservation();
    });
  }
  async function lookup(reference) {
    if (!reference) return;
    root.querySelector("#lookup-feedback").innerHTML = "";
    root.querySelector("#reservation-result").innerHTML = '<p class="loading" role="status">Looking up your reservation…</p>';
    try {
      const result = await api(`/reservations/${encodeURIComponent(reference)}`);
      if (!result.response.ok) throw new Error(apiError(result));
      const record = result.payload;
      const restaurant = await getRestaurant(record.restaurant_id);
      renderReservation(record, restaurant);
    } catch (error) {
      root.querySelector("#reservation-result").innerHTML = "";
      root.querySelector("#lookup-feedback").innerHTML = feedback("reservation-error", error.message || "Reservation not found.", "error");
    }
  }
  function renderReservation(record, config) {
    const ids = record.table_ids || [record.table_id];
    const cancelled = record.status === "cancelled";
    root.querySelector("#lookup-feedback").innerHTML = "";
    root.querySelector("#reservation-result").innerHTML = `
      <section class="detail-card" data-testid="reservation-detail" aria-live="polite">
        <p class="eyebrow">Reservation details</p><h2>${escapeHtml(config.name)}</h2>
        <dl class="detail-lines">
          <div><dt>Status</dt><dd><span class="status-pill ${cancelled ? "cancelled" : ""}" data-testid="reservation-status">${escapeHtml(record.status)}</span></dd></div>
          <div><dt>Tables</dt><dd data-testid="reservation-tables">${escapeHtml(labelsFor(config, ids))}</dd></div>
          <div><dt>Date and time</dt><dd>${escapeHtml(record.starts_at_local.replace("T", " at "))}</dd></div>
          <div><dt>Party size</dt><dd>${escapeHtml(record.party_size)}</dd></div>
          <div><dt>Reference</dt><dd>${escapeHtml(record.reference)}</dd></div>
        </dl>
        ${cancelled ? "" : '<button class="button secondary" data-testid="reservation-cancel-button" type="button">Cancel reservation</button>'}
      </section>`;
    root.dataset.currentReference = record.reference;
    root.dataset.restaurantId = config.id;
  }
  async function cancelReservation() {
    const reference = root.dataset.currentReference;
    if (!reference) return;
    const button = root.querySelector('[data-testid="reservation-cancel-button"]');
    if (button) button.disabled = true;
    try {
      const result = await api(`/reservations/${encodeURIComponent(reference)}/cancel`, { method: "POST", body: "{}" });
      if (!result.response.ok) throw new Error(apiError(result));
      const config = await getRestaurant(result.payload.restaurant_id || root.dataset.restaurantId);
      renderReservation(result.payload, config);
    } catch (error) {
      root.querySelector("#lookup-feedback").innerHTML = feedback("reservation-error", error.message || "This reservation could not be cancelled.", "error");
      button?.removeAttribute("disabled");
    }
  }

  async function render() {
    session = readSession();
    switch (currentRoute()) {
      case "/signup": await renderAuth(true); break;
      case "/login": await renderAuth(false); break;
      case "/lookup": await renderLookup(); break;
      default: await renderSearch();
    }
  }
  window.addEventListener("popstate", render);
  document.addEventListener("click", (event) => {
    const link = event.target.closest('a[href^="/"]');
    if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    history.pushState({}, "", link.getAttribute("href"));
    render();
  });
  render();
})();
