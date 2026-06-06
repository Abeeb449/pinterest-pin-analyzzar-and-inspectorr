# Pinterest Pin Intelligence Tool

A small, password-gated web app for personal competitive research on a
home-decor blog. Two features:

1. **Pin lookup** — paste a Pinterest pin URL (or `pin.it` short link) and get
   its available metadata: board + link, pinner + profile, publish date,
   title/description, save count (best-effort), reactions (if any), comment
   count, and three *distinct* keyword sources — Pinterest's ML-derived
   **keyword annotations**, the pinner's **hashtags**, and the ML **dominant
   interest**. Plus Copy-JSON.
2. **Find similar pins** — visual ("flashlight") search: paste a pin URL and get
   a grid of visually similar pins. Runs as a read (no image upload).

Frontend and backend ship from the **same FastAPI app** (same origin, no CORS).
It runs **anonymously by default** and degrades gracefully when Pinterest blocks
a request — it never crashes and never fabricates data.

> ⚠️ **This is fragile by design.** It talks to Pinterest's *unofficial,
> internal* endpoints (the ones the website itself calls) — the only way to get
> this data, since the official Pinterest API only returns your own pins and has
> no visual search. Those endpoints are undocumented and change without notice,
> so the app may break at any time. See [Important notice](#important-notice).

---

## Tech stack

- Python 3.11, FastAPI + uvicorn
- httpx (persistent client + cookie jar, HTTP/2)
- Plain HTML + vanilla JS + CSS, served by FastAPI as static files
- Config via environment variables (python-dotenv for local dev)
- Deploys to Render's free tier via `render.yaml`

---

## 1. Local setup

Requires Python 3.11+.

```bash
# clone, then from the repo root:
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# configure secrets
cp .env.example .env
# edit .env and set at least APP_PASSWORD and SESSION_SECRET
#   SESSION_SECRET: python -c "import secrets; print(secrets.token_hex(32))"

# run
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 , log in with your `APP_PASSWORD`, and try a pin URL.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `APP_PASSWORD` | ✅ | Password for the login gate. |
| `SESSION_SECRET` | ✅ | Signs the login session cookie. Use a long random string. |
| `PINTEREST_SESS_COOKIE` | optional | `_pinterest_sess` cookie (Phase 2 reliability lever). Leave **unset** to start. |
| `PINTEREST_CSRF_TOKEN` | optional | `csrftoken` cookie, paired with the session cookie. |
| `PINTEREST_REQUEST_DELAY` | optional | Min delay (seconds) between outbound Pinterest requests. Default `1.0`. |
| `DEBUG_ENDPOINTS` | optional | Set to `1`/`true` to enable the debug endpoints (`/api/pin/raw`, `/api/resource/probe`). **Off by default** — they return 404 otherwise. |

The app runs correctly with both Pinterest cookie vars **unset/empty** —
anonymous mode is the default and first-shipped behavior.

### How it works (the parts worth knowing)

- **Reliability vs. richness, by request.** The main fetch uses your session
  cookie (when set) for reliability and rich stats (saves, reactions, comment
  count). But Pinterest *hides* its ML keyword annotations on the logged-in
  view, so the app makes a **second, cookie-free (anonymous) request** purely to
  collect `pin_join.visual_annotation` — then merges it in. Two clients, one
  result.
- **Three keyword sources are kept separate** and never conflated:
  - **Keyword annotations (ML)** — inferred by Pinterest from the image/title/
    description (e.g. "Zig Zag Mirror"). Present on most pins.
  - **Hashtags** — pinner-authored (`#homedecor`); not all pins have them.
  - **Dominant interest** — Pinterest's single ML category label.
- **Visual search is a read.** It calls Pinterest's "flashlight" endpoint by
  pin id (`/v3/visual_search/flashlight/pin/<id>/`) — there is **no image
  upload**, so it carries the same low risk as a normal page view.
- **Graceful fallback ladder for pin lookup:** rich JSON API → authenticated
  embedded page state → oEmbed → public HTML scrape. A partial result always
  beats a hard block; missing fields are noted, never fabricated.

### Dev helper: confirm field paths

Pinterest's internal JSON shape can drift. To dump the raw JSON for one pin
(useful for verifying field paths against the parser):

```bash
python scripts/dump_pin.py "https://www.pinterest.com/pin/<id>/"
```

> Note: networks that block `pinterest.com` (some CI/sandbox egress policies)
> will return a block error here — run it from a normal network.

---

## 2. Grabbing your Pinterest cookies (optional — Phase 2 only)

You only need this **if anonymous mode proves too unreliable** on your
deployment. It improves reliability but raises blocking/exposure risk, so use a
**throwaway** Pinterest account, never your main one.

1. Log in to the throwaway account at pinterest.com in your browser.
2. Open DevTools (F12) → **Application** (Chrome) or **Storage** (Firefox) →
   **Cookies** → `https://www.pinterest.com`.
3. Copy the **Value** of:
   - `_pinterest_sess` → set as `PINTEREST_SESS_COOKIE`
   - `csrftoken` → set as `PINTEREST_CSRF_TOKEN`
4. Set both as environment variables (locally in `.env`, or in the Render
   dashboard) and restart/redeploy. **No code change is needed** — the client
   uses them automatically when present.

Treat these like passwords. Never commit them.

---

## 3. Deploy to Render (free tier)

The repo includes `render.yaml`, so Render can provision everything as a
**Blueprint**.

1. Push this repo to GitHub (already done if you're reading this there).
2. Go to <https://dashboard.render.com> → **New** → **Blueprint**.
3. Connect your GitHub account and select this repository.
4. Render reads `render.yaml` and proposes one free **Web Service**
   (`pinterest-pin-intelligence`). Confirm/apply the blueprint.
5. When prompted for the env vars marked `sync: false`, set **for the first
   deploy**:
   - `APP_PASSWORD` → your chosen password
   - `SESSION_SECRET` → a long random string
     (`python -c "import secrets; print(secrets.token_hex(32))"`)
   - Leave `PINTEREST_SESS_COOKIE` and `PINTEREST_CSRF_TOKEN` **blank**
     (anonymous mode).
6. Deploy. When the build finishes, open the public `*.onrender.com` URL, log
   in, and look up a real pin to verify it works.

Future `git push` to the connected branch redeploys automatically.

### Render notes
- The free Web Service **sleeps after inactivity** and takes a short while to
  cold-start on the next visit.
- Render's free tier uses **shared datacenter IPs** that Pinterest often flags,
  so requests may be rate-limited or blocked. The app degrades gracefully and
  tells you when this happens. If reliability is too poor, add the throwaway
  cookie (section 2) — a pure config change, no rebuild.

---

## Architecture

```
app/
  main.py              # FastAPI app: auth, routes, serves static frontend
  auth.py              # password gate + signed-session dependency
  config.py            # env-var config (anonymous unless cookies set)
  pinterest/
    client.py          # persistent httpx clients (authenticated + cookie-free
                       #   anonymous), warmup, UA rotation, throttle, backoff
    pin.py             # pin fetch + defensive raw -> PinData mapping;
                       #   anonymous annotation enrichment; fallback ladder
    visual.py          # isolated "flashlight" visual search (no upload)
    models.py          # PinData, ImageMatch, Comment (pydantic)
  static/              # index.html, app.js, style.css
scripts/dump_pin.py    # dev helper: dump raw pin JSON
render.yaml            # Render blueprint
runtime.txt            # pinned Python version
requirements.txt
.env.example
```

---

## Data honesty

- **Saves/repins** are best-effort. If Pinterest doesn't return them, the app
  shows `unavailable` — it never estimates.
- **Likes don't exist.** Pinterest removed likes in 2017. The app shows a
  `reactions` count only if one is actually present; there is no fake `likes`.
- **Hashtags ≠ keyword annotations.** Pinner-authored hashtags and Pinterest's
  ML keyword annotations are shown as separate fields and never merged. When the
  ML annotations aren't in a response, the app says so rather than backfilling
  from hashtags.
- On a blocked request or missing field, the app returns partial data plus a
  clear status message rather than crashing or inventing values.

---

## Important notice

This tool uses Pinterest's **unofficial internal endpoints** for **personal
research**. Consequences of that:

- It is **fragile** and may break without warning when Pinterest changes things.
- **Free shared-IP hosting may get blocked** by Pinterest.
- **You are responsible for complying with Pinterest's Terms of Service.**
- Automating requests against your own logged-in account can risk rate-limits or
  flags on that account — which is why a **throwaway** account is recommended if
  you add a session cookie at all.
