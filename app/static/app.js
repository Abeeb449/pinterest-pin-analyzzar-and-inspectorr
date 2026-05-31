// Frontend controller: gate the app behind the password login, then show the
// tool UI. Talks only to the same-origin API (no CORS).
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

  function show(view) {
    loginView.classList.toggle("hidden", view !== "login");
    appView.classList.toggle("hidden", view !== "app");
  }

  function applyMode(mode) {
    modeBadge.textContent = mode;
    modeBadge.classList.remove("anonymous", "authenticated");
    modeBadge.classList.add(mode);
  }

  // Decide which view to show by probing the authenticated endpoint.
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
      /* fall through to login */
    }
    show("login");
    passwordInput.focus();
  }

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
      /* ignore — we clear the view regardless */
    }
    show("login");
    passwordInput.focus();
  }

  loginForm.addEventListener("submit", handleLogin);
  logoutBtn.addEventListener("click", handleLogout);

  checkSession();
})();
