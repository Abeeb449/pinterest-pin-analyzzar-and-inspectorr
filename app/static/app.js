// Frontend controller: gate the app behind the password login, then drive the
// tool panels. Talks only to the same-origin API (no CORS).
(function () {
  "use strict";

  const loginView = document.getElementById("login-view");
  const appView = document.getElementById("app-view");
  const loginForm = document.getElementById("login-form");
  const passwordInput = document.getElementById("password");
  const loginError = document.getElementById("login-error");
  const loginBtn = document.getElementById("login-btn");
  const logoutBtn = document.getElementById("logout-btn");
  const modeBadge = document.getElementById("mode-badge");

  const pinForm = document.getElementById("pin-form");
  const pinUrl = document.getElementById("pin-url");
  const pinBtn = document.getElementById("pin-btn");
  const pinStatus = document.getElementById("pin-status");
  const pinResult = document.getElementById("pin-result");

  // --- view + mode helpers ---
  function show(view) {
    loginView.classList.toggle("hidden", view !== "login");
    appView.classList.toggle("hidden", view !== "app");
  }

  function applyMode(mode) {
    modeBadge.textContent = mode;
    modeBadge.classList.remove("anonymous", "authenticated");
    modeBadge.classList.add(mode);
  }

  async function checkSession() {
    try {
      const res = await fetch("/api/session", { credentials: "same-origin" });
      if (res.ok) {
        const data = await res.json();
        applyMode(data.mode);
        show("app");
        return;
      }
    } catch (_) {
      /* fall through */
    }
    show("login");
    passwordInput.focus();
  }

  // --- auth ---
  async function handleLogin(event) {
    event.preventDefault();
    loginError.textContent = "";
    loginBtn.disabled = true;
    loginBtn.textContent = "Logging in…";
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ password: passwordInput.value }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        passwordInput.value = "";
        await checkSession();
      } else {
        loginError.textContent = data.error || "Login failed.";
      }
    } catch (_) {
      loginError.textContent = "Network error. Try again.";
    } finally {
      loginBtn.disabled = false;
      loginBtn.textContent = "Log in";
    }
  }

  async function handleLogout() {
    try {
      await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
    } catch (_) {
      /* ignore */
    }
    show("login");
    passwordInput.focus();
  }

  // --- pin lookup ---
  function setStatus(kind, message) {
    pinStatus.className = "status" + (kind ? " " + kind : "");
    pinStatus.textContent = message || "";
  }

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function link(href, text) {
    if (!href) return esc(text);
    return `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(text)}</a>`;
  }

  function chipBlock(label, items) {
    if (!items || !items.length) return "";
    const chips = items.map((a) => `<span class="chip">${esc(a)}</span>`).join("");
    return `<div class="chip-group"><div class="chip-label">${esc(label)}</div><div class="chips">${chips}</div></div>`;
  }

  function renderPin(pin) {
    const saves = pin.saves == null ? "<em>unavailable</em>" : esc(pin.saves);
    const reactions =
      pin.reactions == null ? "" : `<div class="kv"><span>Reactions</span><b>${esc(pin.reactions)}</b></div>`;

    // Three DISTINCT keyword sources, shown separately so they're never
    // confused: Pinterest's ML annotations vs the pinner's hashtags vs the
    // single ML dominant-interest category.
    const annotationChips = chipBlock("Pinterest keyword annotations (ML)", pin.annotations);
    const hashtagChips = chipBlock("Hashtags (pinner-authored)", pin.hashtags);
    const interestRow = pin.dominant_interest
      ? `<div class="kv"><span>Dominant interest</span><b>${esc(pin.dominant_interest)}</b></div>`
      : "";

    const notes = (pin.notes || []).length
      ? `<div class="notes">Notes: ${pin.notes.map(esc).join("; ")}</div>`
      : "";

    pinResult.innerHTML = `
      <div class="card">
        ${pin.image ? `<img class="card-img" src="${esc(pin.image)}" alt="" />` : ""}
        <div class="card-body">
          <h3>${pin.url ? link(pin.url, pin.title || "(untitled)") : esc(pin.title || "(untitled)")}</h3>
          ${pin.description ? `<p class="desc">${esc(pin.description)}</p>` : ""}
          <div class="kv"><span>Board</span><b>${link(pin.board_url, pin.board_name || "—")}</b></div>
          <div class="kv"><span>Pinner</span><b>${link(pin.pinner_url, pin.pinner_name || "—")}</b></div>
          <div class="kv"><span>Published</span><b>${esc(pin.created_at || "—")}</b></div>
          <div class="kv"><span>Saves</span><b>${saves}</b></div>
          ${reactions}
          <div class="kv"><span>Comments</span><b>${esc(pin.comment_count == null ? "—" : pin.comment_count)}</b></div>
          ${interestRow}
          ${annotationChips}
          ${hashtagChips}
          ${notes}
          <button type="button" class="ghost-btn" id="copy-json">Copy JSON</button>
        </div>
      </div>`;
    pinResult.classList.remove("hidden");

    const copyBtn = document.getElementById("copy-json");
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(JSON.stringify(pin, null, 2));
        copyBtn.textContent = "Copied!";
        setTimeout(() => (copyBtn.textContent = "Copy JSON"), 1500);
      } catch (_) {
        copyBtn.textContent = "Copy failed";
      }
    });
  }

  async function handlePinLookup(event) {
    event.preventDefault();
    pinResult.classList.add("hidden");
    pinResult.innerHTML = "";
    pinBtn.disabled = true;
    setStatus("loading", "Looking up pin…");
    try {
      const res = await fetch("/api/pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ url: pinUrl.value }),
      });
      if (res.status === 401) {
        show("login");
        return;
      }
      const data = await res.json().catch(() => ({}));
      if (data.ok && data.pin) {
        setStatus("", "");
        renderPin(data.pin);
      } else {
        const kind = data.status === "blocked" ? "blocked" : "error";
        setStatus(kind, data.message || "Lookup failed.");
      }
    } catch (_) {
      setStatus("error", "Network error. Try again.");
    } finally {
      pinBtn.disabled = false;
    }
  }

  loginForm.addEventListener("submit", handleLogin);
  logoutBtn.addEventListener("click", handleLogout);
  pinForm.addEventListener("submit", handlePinLookup);

  checkSession();
})();
