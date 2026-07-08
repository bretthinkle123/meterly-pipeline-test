# src/web/

## Purpose

Served static web UI: HTML, CSS, and JavaScript for the read-only usage dashboard (`SCREEN-1`), a client-side controller that drives state changes and renders data fetched from the BFF without exposing credentials to the browser.

## Modules

| File / Module | Responsibility |
|---|---|
| `static/dashboard.html` | SCREEN-1 markup: semantic `<table>` for the "Recent windows" data table, native `<select>` elements for Customer/Metric dropdowns (styled), segmented-control button group for Window granularity (hour/day/month with month disabled), stat card for current usage, empty-state card, and skeleton-loading placeholders. External CSS/JS links only (no inline scripts). |
| `static/dashboard.css` | Token → CSS port of all design components (`CMP-1`…`CMP-9`): app header, environment badge, custom-styled select/segmented-control, stat card, delta pill (up/down/neutral variants), data table with grid columns, empty-state and error-card layouts, loading skeleton states. Hover/focus interactive states for form controls. No inline styles or `unsafe-inline` directives. |
| `static/dashboard.js` | Vanilla-JS controller: fetch config (allowlists, environment, enabled granularities) and usage series, render populated/empty/loading/error states via `textContent`/`createElement` (never `innerHTML`), wire filter change (customer/metric/granularity select handlers) to trigger re-fetch + re-render. Single-screen state machine; no client-side routing. XSS-safe sink (output encoding at the DOM boundary). |

## Render States

The dashboard displays four render states, all defined in the CSS + JS controller logic:

1. **Populated** — stat card + "Recent windows" table: displayed when the selected customer/metric/granularity has usage data.
2. **Empty** — centered empty-state card: displayed when all windows for the selection are zero.
3. **Loading** — skeleton placeholder: shown on initial page load and on every filter change (customer/metric/granularity). Mirrors the populated layout (stat card + 10 table rows) with muted colors and optional subtle pulse. Layout-preserving, no content shift.
4. **Error** — error card (reuses empty-state card structure): shown if the BFF fetch fails (non-200 response or network error). Generic message "Couldn't load usage" + Retry button.

## Data Flow

```
User opens /dashboard
  ↓ (load)
fetch /dashboard/api/config
  ↓
populate dropdowns + environment badge from config
  ↓
fetch /dashboard/api/usage-series?customer_id=...&metric=...&granularity=...
  ↓ (on success)
render Populated state (or Empty if all windows are zero)
  ↓ (user changes filter: customer, metric, or granularity)
show Loading skeleton
  ↓
fetch new /dashboard/api/usage-series with new params
  ↓ (on success)
render Populated/Empty as appropriate
```

## XSS Defense

Every dynamic value (customer_id, metric, quantity, window labels, deltas) is written to the DOM via **`textContent` and `document.createElement`, never `innerHTML`, inline event handlers, or `eval`**. This is the output-encoding-at-the-DOM-sink defense:

- `textContent` treats all strings as text, never as markup.
- No inline `onclick` / `onload` attributes; handlers are bound via `.addEventListener`.
- No template literals or string concatenation into HTML; DOM construction is explicit.

Backed by the strict served-page CSP (`script-src 'self'`, no `unsafe-inline`/`unsafe-eval`) and the boundary allowlists (customer_id/metric membership), so a value that somehow reached the DOM still cannot execute.

## Security Headers (Set by Middleware)

- **`Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; …`** — permits only the page's own self-hosted assets, blocks inline scripts and unsafe directives.
- **`Cache-Control: no-store`** — prevents browser/intermediary caching of usage data (ASVS 14.3.2).
- **`X-Frame-Options: DENY`** + **CSP `frame-ancestors 'none'`** — prevents clickjacking (ASVS 3.4.6).
- **`Referrer-Policy: strict-origin-when-cross-origin`** — limits referrer leakage (ASVS 3.4.5).
- **`X-Content-Type-Options: nosniff`** — disables MIME sniffing (ASVS 3.4.4).
- **`Strict-Transport-Security` (prod only)** — enforces HTTPS (ASVS 3.4.1).

## Design Source

Ported from SCREEN-1 of the human-vouched `.pipeline/design-spec.md` (Claude Design export). Every visual component maps to a design token (color, type, radius, elevation) and a design-spec id (`CMP-1` app header, `CMP-2` environment badge, etc.). The render states (loading, error) are designed in the same component language since the export depicted only populated/empty.

## Relationships

**Public surface:**
- Served by `src/api.routes.dashboard` via explicit `FileResponse` routes (no `StaticFiles` mount; fixed set of three known assets).

**Dependency:**
- `dashboard.js` fetches from the same-origin BFF (`/dashboard/api/*` routes) — no credential in the browser; the BFF holds the server-side reader key and returns the assembled usage series as JSON.
- The page and assets inherit all middleware (request-ID, security headers, `Cache-Control: no-store`, CSP, Tier-1 throttle). No additional auth is required for the viewer; the app-layer BFF is unauthenticated for the viewer (credentials are server-held).

## Notes

**Why explicit `FileResponse` routes over `StaticFiles` mount:**
- A mounted `StaticFiles` serves any file under its directory from a user-controlled path (path-traversal surface, ASVS V5).
- For a fixed set of three known assets (`dashboard.html`, `dashboard.css`, `dashboard.js`), explicit routes eliminate that surface entirely.
- Security headers and CSP logic are uniform with the rest of the app (set once in middleware, respected by all routes).

**Why vanilla HTML/CSS/JS over a framework:**
- The Claude Design export is already vanilla HTML/CSS/JS; porting it to static assets + a small vanilla-JS controller is the **closest 1:1 port**.
- No server-side templating needed (the design is an **in-screen re-render** screen, not a multi-page app).
- No new runtime dependency (FastAPI's `FileResponse` is built in).
- A React/Vue SPA would add a Node build toolchain, a new deploy artifact, and a cross-origin (CORS) boundary the API deliberately forbids.

**Why no inline styles in the export:**
- The export's `style-hover`/`style-inline` syntax translates to real `:hover`/`:focus` CSS rules and external `<style>` blocks — no `unsafe-inline` CSP directive needed.

**Accessibility (a11y):**
- Native `<select>` and `<button>` elements preserve keyboard navigation and screen-reader semantics.
- Segmented-control buttons are keyboard-operable (Tab, arrow keys, Space/Enter to select).
- Table uses semantic `<table>` markup (header row, caption for a11y context).
- Skip links and ARIA labels are present where needed (see design-spec §6 open items).
