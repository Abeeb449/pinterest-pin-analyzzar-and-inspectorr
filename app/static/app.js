// Phase 1 scaffold frontend. Fetches health to confirm same-origin API works
// and shows whether the backend is in anonymous or authenticated mode.
(function () {
  "use strict";

  async function refreshMode() {
    const badge = document.getElementById("mode-badge");
    try {
      const res = await fetch("/healthz");
      const data = await res.json();
      badge.textContent = data.mode;
      badge.classList.remove("anonymous", "authenticated");
      badge.classList.add(data.mode);
    } catch (err) {
      badge.textContent = "offline";
    }
  }

  refreshMode();
})();
