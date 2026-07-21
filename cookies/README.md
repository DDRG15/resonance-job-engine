# cookies/ — Session Cookies

Two mechanisms live here, kept separate on purpose:

1. **CookieVault (manual)** — `cookies/<domain>.json` files you export from Chrome
   for login-gated boards. You manage these by hand. Documented below.
2. **SessionJar (automatic)** — `cookies/session/<domain>.json`, written by GEMA
   itself. Do NOT touch these. See the next section.

**All `*.json` files here (and the whole `session/` subdir) are gitignored and
NEVER committed to GitHub.**

---

## `cookies/session/` — automatic cookie jar (do not edit)

GEMA captures the browser's session/clearance cookies AFTER each scrape and reuses
them on the next run, so anti-bot clearance (Cloudflare `cf_clearance`, `__cf_bm`,
etc.) survives without you exporting anything or using credentials. Cloudflare
issues `cf_clearance` after the browser passes the challenge once; persisting it
skips the challenge next time.

- Written and read automatically by `integrations/session_jar.py`. One file per
  board, in Playwright cookie format.
- Fine print (by design, not a bug): `cf_clearance` is tied to IP + User-Agent, so
  it has a limited lifetime and does not transfer between machines/IPs — it helps
  on the same machine over hours/days. Best-effort: an expired cookie simply
  doesn't help, it never breaks the scrape.
- Safe to delete the whole `session/` folder any time; GEMA rebuilds it on the
  next run. Keep it out of the repo (already gitignored).

The manual `CookieVault` (below) is injected AFTER the jar, so a hand-exported
login cookie always wins over an auto-captured one for the same board.

---

## Supported Boards (manual CookieVault)

| Board | File | Why needed |
|-------|------|------------|
| wellfound.com | `wellfound.com.json` | Login wall — returns 0 jobs without auth |
| arc.dev | `arc.dev.json` | Login wall — returns 0 jobs without auth |
| welcometothejungle.com | `welcometothejungle.com.json` | Login + bot detection |

---

## How to Export Cookies from Chrome

### Method A — Chrome DevTools (no extension)

1. Log into the target site in Chrome
2. Open DevTools (F12) → **Application** tab → **Cookies** → click the site URL
3. Select all rows (Ctrl+A), right-click → **Copy all as JSON**
4. Paste into a file named `<domain>.json` in this directory

### Method B — EditThisCookie Extension (easier)

1. Install [EditThisCookie](https://chrome.google.com/webstore/detail/editthiscookie/fngmhnnpilhplaeedifhccceomclgfbg)
2. Navigate to the target site while logged in
3. Click the EditThisCookie icon → **Export** (clipboard icon)
4. Paste into `<domain>.json` in this directory

---

## Expected Format

JSON array of cookie objects (Chrome DevTools format):

```json
[
  {
    "name": "session_token",
    "value": "eyJ...",
    "domain": ".wellfound.com",
    "path": "/",
    "expirationDate": 1748000000.0,
    "httpOnly": true,
    "secure": true,
    "sameSite": "Lax"
  }
]
```

GEMA's `CookieVault` handles both Chrome DevTools format (`expirationDate`) and
Playwright format (`expires`) automatically.

---

## Verification

After placing a cookie file, verify it loads correctly:

```bash
cd gema-v2.1-extraction/gema
python -c "
from integrations.cookie_vault import CookieVault
v = CookieVault()
print('Domains with cookies:', v.list_domains())
print('wellfound has cookies:', v.has_cookies('wellfound.com'))
"
```
