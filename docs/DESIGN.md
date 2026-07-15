# babyg frontend design system

The single source-of-truth doc for the babyg frontend. Use it as a
reference catalog before adding new pages, components, or styles —
the goal is one consistent visual language across creator, brand,
operator, and marketing surfaces, not parallel design systems per
role.

The system is **mobile-first**, **dark-only**, and built on
**server-rendered Jinja templates** with **vanilla JS** for
progressive enhancement (no framework, no build step).

---

## 1. Where the frontend lives

```
app/
├── templates/
│   ├── base.html                       # Per-role shell switch
│   ├── error.html                      # 404 / 500 page
│   ├── _partials/                      # Shared cross-template pieces
│   │   ├── _avatar.html
│   │   ├── brand_mark.html             # Logo lockup macros (wordmark, icon)
│   │   ├── brand_tabbar.html           # Brand-role bottom nav (5 tabs)
│   │   ├── bot_messages.html
│   │   ├── creator_card.html           # Shared creator preview card
│   │   ├── creator_mobile_header.html  # Creator mobile topbar (back / title / icon-btn)
│   │   ├── creator_tabbar.html         # Creator bottom nav (5 tabs)
│   │   ├── csrf.html                   # CSRF hidden input — include in every <form>
│   │   ├── dm_thread_messages.html
│   │   ├── integrations_grid.html      # Onboarding/settings integrations row
│   │   ├── intel_card.html             # Hot Drop / intel card
│   │   ├── job_card.html
│   │   ├── operator_topbar.html
│   │   └── report_form.html
│   ├── marketing/                      # /, /get-started — public surfaces
│   ├── auth/                           # login, code, callback, sent, error
│   ├── onboarding/                     # creator, brand, operator wizards
│   ├── creator/                        # All /creator/* pages
│   ├── brand/                          # All /brand/* pages
│   ├── operator/                       # All /operator/* pages
│   └── legal/                          # Privacy, terms, etc. (scoped CSS)
│
└── static/
    ├── css/
    │   ├── app.css                     # In-app surfaces (creator/brand/operator/auth/onboarding)
    │   ├── landing.css                 # /lp scoped — never leaks into the app
    │   ├── landing-task-cards.css      # Landing extension; loaded after landing.css
    │   └── legal.css                   # .legal-pane scoped
    ├── js/                             # Per-page progressive-enhancement scripts
    │   ├── motion.js                   # Reveal-on-scroll, loaded globally
    │   ├── auth_callback.js
    │   ├── bot.js                      # Async chat composer
    │   ├── discover.js                 # Filter chip behavior on /discover
    │   ├── dm_briefs.js                # "use draft" + "ask babyg" in DM thread
    │   ├── landing.js                  # Landing reveal animations
    │   ├── network_connections.js      # Disconnect confirmation
    │   ├── network_swipe.js            # Swipe gestures + keyboard arrows
    │   ├── onboarding.js               # Wizard stepper
    │   └── profile.js                  # Client-side photo compressor
    └── assets/
        ├── logo-bg.png                 # Primary mark
        └── logo-bg-dark.png            # Dark-bg variant
```

**One rule, no exceptions**: `landing.css` and `legal.css` are scoped
(`.lp *`, `.legal-pane *`). `app.css` covers everything else. Do not
introduce a fourth in-app stylesheet — extend `app.css` with a new
section comment instead.

---

## 2. Design tokens

All exposed as CSS variables at `:root` in `app.css` lines 7–47.

### Color

| Token | Hex / value | Used for |
| --- | --- | --- |
| `--obsidian` | `#0A0A0A` | Page background |
| `--jet` | `#050505` | Deeper surface (rare) |
| `--bone` | `#F5F1E8` | Primary text |
| `--bone-warm` | `#EEE7D6` | Warm text variant |
| `--smoke` | `#888888` | Neutral mid |
| `--ash` | `#1c1c1c` | Quiet surface accent |
| `--surface-1` | `#111111` | Card background (default) |
| `--surface-2` | `#161616` | Card background (frosted variant) |
| `--surface-3` | `#1c1c1c` | Nested card / input background |
| `--hairline` | `#1d1d1d` | Default 1px border |
| `--hairline-strong` | `#2a2a2a` | Stronger 1px border |
| `--ink` | `var(--bone)` | Body text |
| `--ink-mid` | `#b8b3a8` | Secondary text |
| `--ink-dim` | `#7a766f` | Tertiary text / labels |
| `--ink-faint` | `#4a4742` | Quaternary text |
| `--lime` | `#FF4D6D` | Primary accent (hot pink, not actually lime) |
| `--lime-dim` | `#E0395B` | Hover / pressed variant |
| `--lime-soft` | `rgba(255,77,109,0.12)` | Filled accent background |
| `--accent` | `#e85a4f` | Secondary accent (coral) |
| `--accent-bg` | `rgba(232,90,79,0.10)` | Coral filled |
| `--chrome-1` | `#d8dde6` | Chrome accent light |
| `--chrome-2` | `#8e9cb8` | Chrome accent mid |

**Important naming quirk**: `--lime` is hot pink. Historical name kept
for backwards compatibility — class names like `.btn-lime`,
`.lime-soft`, etc. all render pink/coral. Do not rename without a
codebase-wide pass; do not introduce a new `--pink` variable.

### Typography

```css
--sans: "Helvetica Neue", "Inter", system fonts
--mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo

--display-track: -0.045em        /* tight tracking on headlines */
--display-weight: 500             /* never use 700/bold for display */
```

Body default: `15px / 1.55`, weight 400.
Display headlines: `clamp(...)` between 22–48px, `font-weight: 500`,
`letter-spacing: var(--display-track)`.
Mono is used for **labels, eyebrows, timestamps** only — never for
body copy. Mono is rendered lowercase via `text-transform: lowercase`
on the `.mono` class.

### Spacing + dimensions

```css
--tabbar-h: 76px      /* mobile bottom nav height */
--topbar-h: 64px      /* desktop / brand topbar height */
--sidebar-w: 240px    /* creator desktop sidebar */
```

Spacing scale is **not formalized** — most padding/gap values use
`6–24px` literals. Standard rhythm:

- card padding: `14px 16px` → `18px` → `22px`
- inter-card gap: `10px` (tight) → `18px` (default) → `26px` (loose)
- text-block stack: `6–10px`
- border-radius: `8px` (inputs), `10–14px` (cards), `16–22px` (large cards), `999px` (pills)

### Layout containers

```css
.wrap         { max-width: 1280px; padding: 0 24px; }
.wrap-wide    { max-width: 1440px; padding: 0 24px; }
.wrap-narrow  { max-width: 720px;  padding: 0 24px; }
```

Every in-app page wraps its content in one of these. Default is
`.wrap`. Use `.wrap-narrow` for long-form text (legal pages,
single-column forms). Use `.wrap-wide` for marketing.

---

## 3. Layout shells

`base.html` switches the shell by URL prefix (`is_creator`,
`is_brand`, `is_operator`, `is_marketing`). Every page extends
`base.html` and writes into `{% block content %}`.

### Creator shell — `is_creator`

`base.html:24–115`

- Desktop topbar (`.app-topbar`): brand-lockup left, avatar link right
- **Desktop sidebar** (`.app-sidebar`): 5-item nav + babyg-card promo
  (visible ≥1000px wide)
- **Mobile bottom tabbar** (`.app-tabbar`): same 5 items (visible <1000px)
- Main content (`.app-main`) + `#view > .view` wrapper
- Watermark backdrop (`.app-bg-watermarks`)

### Brand shell — `is-brand-shell`

`base.html:117–144`, CSS at `app.css:1710–1731`

- Topbar with brand-lockup + sign-out button
- **No sidebar.** Bottom tabbar visible at all widths (CSS override at
  `app.css:1723`: `.is-brand-shell .app-tabbar { display: flex }`)
- 5-tab brand bottom nav

### Operator shell — `operator-shell`

`base.html:146–155`

- Operator topbar partial
- `.operator-main` content
- No tabbar — operator is a desktop-first console

### Marketing / auth shell — `is-marketing`

`base.html:157–163`

- No topbar, no sidebar, no tabbar
- Single `.app-main` column
- Used by `/`, `/auth/*`, `/get-started`

---

## 4. Navigation

### Tab data-attribute convention

Every nav link carries a `data-tab="<slot>"` attribute. The CSS at
`app.css:412–418` and `app.css:493–500` maps each slot to an accent
color via three vars: `--nav-accent`, `--nav-accent-soft`, `--nav-glow`.

| `data-tab` | accent hex | Used for |
| --- | --- | --- |
| `feed` | `#ff526c` (lime) | Home (creator + brand campaigns) |
| `chat` | `#9ccaff` | Babyg chat |
| `calendar` | `#79aef4` | (legacy slot, no longer in creator tabbar) |
| `inbox` | `#94d7ea` | DMs |
| `network` | `#b9a4ff` | Discover |
| `stats` | `#7edfc4` | (legacy slot — kept for brand saved/operator stats) |
| `profile` | `#ead7b5` | Profile |

When adding a tab, reuse the closest existing `data-tab` value to
inherit the accent. Never invent a new accent color without explicitly
extending the table above.

### Creator bottom nav — 5 tabs

`_partials/creator_tabbar.html`. Order is locked in:

```
Home  ·  Discover  ·  Babyg  ·  DMs  ·  Profile
```

- **Home** (`data-tab="feed"`): `/creator`. Active when path is
  `/creator` OR starts with `/creator/calendar`. Calendar folded under
  Home — the route still exists but doesn't get its own tab.
- **Discover** (`data-tab="network"`): `/creator/discover`. Active for
  `/creator/discover|network|connections|jobs/...`.
- **Babyg** (`data-tab="chat"`): `/creator/bot`. Active for
  `/creator/bot/...`. Uses the `logo-bg.png` icon, not an SVG.
- **DMs** (`data-tab="inbox"`): `/creator/dm`. Active for `/creator/dm/...`.
- **Profile** (`data-tab="profile"`): `/creator/profile`. Active for
  `/creator/profile|settings|performance|receipts|views/...`. Stats
  and integrations live under Profile.

### Brand bottom nav — 5 tabs

`_partials/brand_tabbar.html`. Order:

```
Discover  ·  Campaigns  ·  Messages  ·  Saved  ·  Profile
```

Visible at all widths because the brand shell has no sidebar.

### Operator nav

`_partials/operator_topbar.html`. Top-of-page horizontal nav, no
mobile-specific variant — operator is desktop-first.

---

## 5. Component primitives

Reuse before you build. Every class below is in `app.css`.

### Buttons — `.btn` (lines 121–159)

```html
<a class="btn btn-lime btn-sm">primary action →</a>
<a class="btn btn-ghost btn-sm">secondary</a>
<button class="btn btn-light">on-dark variant</button>
<button class="btn btn-danger">destructive</button>
<a class="btn btn-lime btn-wide">full-width</a>
```

Modifiers stack: `.btn .btn-lime .btn-sm`. Variants:

| Modifier | Result |
| --- | --- |
| `.btn-lime` | Solid pink-accent — **primary action** |
| `.btn-ghost` | Transparent + hairline-strong border — secondary |
| `.btn-light` | Bone-on-obsidian inverted |
| `.btn-danger` | Pink outline that fills on hover |
| `.btn-dark` | Obsidian-on-bone (rare) |
| `.btn-sm` | `9px 16px` padding, 12.5px font |
| `.btn-lg` | `15px 28px`, 14.5px font |
| `.btn-wide` | `width: 100%`, centered text |

### Pills + tags — `.chip`, `.tag`, `.badge` (lines 161–203)

- **`.chip`** — interactive filter pill, 7px 14px, 999px radius.
  `.chip-active` for selected state. Used in filter rows and chip
  groups (niches/formats/limits).
- **`.tag`** — compact label, 3px 9px, 6px radius, 9.5px mono. Used
  for niches/limits inside cards (read-only).
- **`.badge`** — uppercase mono tracking, 4px 9px. Variants:
  - `.badge-muted` — secondary
  - `.badge-coral` — accent (lime border + lime-soft fill)
  - `.badge-chrome` — info (blue-tinted)

### Avatar — `.avatar` (lines 2328+)

```html
<span class="avatar av-g3"></span>     <!-- 44px circle, gradient g3 -->
<span class="avatar av-g3 lg"></span>  <!-- 64px -->
<span class="avatar av-g3 xl"></span>  <!-- 84px -->
<span class="avatar avatar-photo">
  <img src="..." />
</span>
```

Gradients `av-g1` through `av-g5` give per-initial variety. Photo
variant strips padding and lets the inner `<img>` fill via
`object-fit: cover`.

### Wraps + page heads

```html
<div class="wrap">
  <div class="brand-page-head">     <!-- shared dashboard head -->
    <div>
      <span class="mono eyebrow">section name</span>
      <h1>title</h1>
      <p>one-line subtitle.</p>
    </div>
    <a href="..." class="btn btn-lime btn-sm">primary →</a>
  </div>
  ...
</div>
```

`.brand-page-head` (`app.css:1734+`) is the canonical dashboard
header. Reuse it on creator-side pages too — the class name is
historical, not role-bound.

### Cards

**`.creator-card`** (`app.css:2355+`)
- 18px padding, 16px radius, hover lift `translateY(-2px)`
- Used in Discover, saved, profile preview, network list
- Static variant: `.creator-card-static` (no hover lift)
- Shared partial: `_partials/creator_card.html`

**`.profile-details-card`** (`app.css:1874+`)
- Frosted surface-2 card with `backdrop-filter: blur(12px)`
- Has `.profile-details-head` (flex row) + `.profile-detail-grid`
  (2-col) + `.profile-detail-item` (min-height 92px tile)
- Used on creator profile, brand profile, brand dashboard

**`.intel-card`** (`app.css:2298+`)
- Hot Drop / intel post — shared partial `_partials/intel_card.html`

### Settings-style forms — `.settings-group`, `.settings-row`, `.settings-form` (lines 4336+)

The canonical pattern for editable sections. Used on:
- `/creator/profile/settings`
- `/brand/profile` (identity + preferences forms)
- `/brand/profile/deals`

```html
<form class="settings-group settings-form" method="post" action="...">
  {% include "_partials/csrf.html" %}
  <h2>section title</h2>
  <p class="settings-help">explanation.</p>
  {% if flash == 'ok' %}<div class="settings-flash settings-flash-ok">saved.</div>{% endif %}

  <div class="settings-row settings-row-stack">
    <label for="x"><span>label</span></label>
    <select id="x" name="x">...</select>
  </div>

  <div class="settings-row settings-row-stack">
    <label class="settings-toggle">
      <input type="checkbox" name="y" value="on" checked />
      <span>checkbox label</span>
    </label>
  </div>

  <div class="settings-actions">
    <button class="btn btn-lime btn-sm" type="submit">save</button>
  </div>
</form>
```

Flash strings: `?<section>=ok` (success), `?<section>=invalid`
(validation), `?<section>=save_failed` (transport).

### KV list — `.kv` (lines 4396+)

For read-only label/value rows in settings tiles.

### Dialog / modal — `.profile-chip-dialog` (lines 2001+)

```html
<dialog class="profile-chip-dialog" data-profile-chip-dialog>
  <form class="profile-chip-sheet" method="post">
    <div class="profile-chip-sheet-head">
      <h2>title</h2>
      <button type="button" data-profile-chip-close>close</button>
    </div>
    <p>helper copy.</p>
    ...
    <div class="profile-chip-actions">
      <button type="button" class="btn btn-ghost btn-sm" data-profile-chip-close>cancel</button>
      <button type="submit" class="btn btn-lime btn-sm">save</button>
    </div>
  </form>
</dialog>
```

JS is in `profile.js` (chip dialog code is in profile.js's third IIFE).
Behaviors:
- `data-profile-chip-open="<section>"` opens `dialog#profile-chip-<section>`
- Click on backdrop closes
- `data-profile-chip-close` button closes
- Submit closes via form action

Always use a native `<dialog>` element (no polyfill). The CSS includes
`::backdrop` styling — every dialog renders with a darkened, blurred
overlay.

### Empty states

Pattern repeated across roles. Standard class set:

| Class | Where | Notes |
| --- | --- | --- |
| `.brand-empty` | `app.css:1843` | Brand pages (DM placeholder, no-saves, no-campaigns) |
| `.home-today-empty` | `app.css:2272` | Home calendar preview empty |
| `.dm-empty`, `.bot-empty-state`, `.posting-empty`, `.performance-empty`, `.alerts-empty` | Various | Per-page variants |

Universal recipe:

```html
<div class="brand-empty">
  <span class="icon-circle" aria-hidden="true"><svg .../></span>
  <h2>nothing here yet</h2>
  <p>one sentence explaining what fills this in.</p>
  <a href="..." class="btn btn-lime btn-sm">primary action →</a>
</div>
```

**Honesty rule**: empty states never imply data exists. No fake stats,
no fake messages, no placeholder names. Always offer a real next step.

### Stat tiles — `.brand-stat-tile`, `.brand-quick-action`

`app.css:1774–1838`. Grid-friendly tile pattern for dashboards:

```html
<div class="brand-stat-grid">
  <a class="brand-stat-tile" href="...">
    <span class="l">label</span>
    <span class="v">42</span>
    <span class="h">hint copy</span>
  </a>
  ...
</div>
```

Responsive: 1col → 2col @560px → 4col @900px.

---

## 6. Forms

### Inputs — used inline (no dedicated class)

The conventional input style is hand-stamped inline:

```html
<input
  type="text"
  style="background:var(--surface-2);
         color:var(--ink);
         border:1px solid var(--hairline-strong);
         border-radius:8px;
         padding:9px 10px;
         font-size:13.5px;
         width:100%;
         max-width:360px;" />
```

This appears in ~9 templates. If you add another, copy this exactly.
A class refactor is a candidate cleanup task — until then, consistency
wins.

### Selects + checkboxes

`.settings-form select` and `.settings-toggle input` have dedicated
styles. Use them inside `.settings-row .settings-row-stack`.

### Chip groups (multi-select)

```html
<div class="chips" role="group" aria-label="...">
  {% for value in vocab %}
  <label class="chip {{ 'chip-active' if value in selected }}">
    <input type="checkbox" name="..." value="{{ value }}"
           {% if value in selected %}checked{% endif %} hidden />
    {{ value.replace('_', ' ') }}
  </label>
  {% endfor %}
</div>
```

Visible label is the chip text; the `<input>` itself is hidden but
ships its value via form submission. The toggle visual relies on
`.chip-active` being applied at render time (no JS toggle).

### Onboarding wizards — `.onb-*`

`app.css:1370+`. Multi-step wizards use `.onb-shell` + `.onb-card`
frosted background + `.onb-progress` step dots + `.onb-section`
fieldsets. The stepper JS in `onboarding.js` toggles `[hidden]` on
each `<fieldset data-onb-step="N">`. **Always works without JS** —
every step is visible if scripts fail.

---

## 7. Marketing & landing

Scoped under `.lp` in `landing.css`. **Never mix `.lp` selectors with
in-app selectors.** The landing reuses the same color palette but
defines its own variables (`--lp-bg`, `--lp-lime`, …) so a tweak to
landing styling can't accidentally repaint the in-app surfaces.

Buttons on landing: `.lp-btn`, `.lp-btn-lime`, `.lp-btn-ghost`,
`.lp-btn-outline`, `.lp-btn-light`. They mirror the in-app buttons but
live under `.lp` namespace.

Landing animation in `landing.js` + `motion.js`. Respects
`prefers-reduced-motion`.

---

## 8. Mobile & responsive

**Mobile-first.** Default styles target 390px. Larger breakpoints
**add** layout, never subtract.

### Breakpoints used (sorted)

| Breakpoint | Used for |
| --- | --- |
| `max-width: 480px` | iPhone SE stacking (e.g. `.home-today-item`) |
| `max-width: 640px` | Onboarding spacing tweaks |
| `max-width: 700px` | Brand main padding tighter |
| `max-width: 720px` | Marketing modules collapse |
| `min-width: 560px` | Stat grids → 2 col |
| `min-width: 700px` | Onboarding hero adjusts |
| `min-width: 800px` | Marketing "how it works" grid |
| `min-width: 900px` | Stat grids → 4 col |
| `min-width: 1000px` | **Sidebar appears, tabbar hides** for creator |
| `min-width: 1070px` | Marketing wider sections |

The `1000px` cut is the only **layout-mode** switch. Below it,
creator and brand both show the bottom tabbar. Above it, creator
shows the sidebar (tabbar hides via `.app-tabbar { display: none }`)
while brand keeps the tabbar (override in `app.css:1723`).

### Non-negotiable mobile rules

- **No horizontal overflow.** Test at 375px. Stat grids, chip rows,
  and forms all wrap.
- **Tap targets ≥44×44px.** Buttons, tab anchors, icon-buttons.
- **Form inputs cap at `max-width: 360–520px`** so they don't blow
  past phone widths.
- **No fixed widths in flexbox containers.** Use `max-width` + `flex:
  1` instead.
- **Bottom tabbar fixed.** Reserve `var(--tabbar-h)` bottom padding on
  every `*-main` so content isn't covered by the nav.
- **`viewport-fit=cover` + `pb-safe` inset**. The tabbar uses
  `env(safe-area-inset-bottom)` for iPhone notch / home-indicator
  clearance.

---

## 9. JavaScript

All vanilla, no framework. Each file is a single IIFE so global
namespace stays clean. **Progressive enhancement is the rule** —
every form must work without JS.

| File | Purpose | Auto-loaded |
| --- | --- | --- |
| `motion.js` | Scroll-reveal class toggling. Respects `prefers-reduced-motion`. | Yes (in `base.html`) |
| `auth_callback.js` | Reads access/refresh tokens from URL fragment, POSTs to `/auth/callback`. | `auth/callback_bridge.html` |
| `bot.js` | Async chat composer + action-card confirm. | `creator/bot.html` |
| `discover.js` | Filter chip toggle on `/discover`. | `creator/discover.html` (where included) |
| `dm_briefs.js` | "use draft" + "ask babyg" inside DM thread. | `creator/dm_thread.html` |
| `landing.js` | Reveal animations on `/`. | `marketing/landing.html` |
| `network_connections.js` | Disconnect-row confirm + fetch. | `creator/connections_list.html` |
| `network_swipe.js` | Swipe + arrow keys on Discover. | `creator/network_swipe.html` |
| `onboarding.js` | Wizard stepper. | `onboarding/creator.html`, `onboarding/brand.html` |
| `profile.js` | Client-side photo compressor + chip-dialog open/close. | `creator/profile.html` |

### Patterns to follow when adding JS

- Wrap in `(() => { ... })()` IIFE
- Bail early if the target element isn't on the page
- Listen to forms via `addEventListener("submit", ...)` and call
  `preventDefault()` only after confirming the JS path will succeed
- Use `fetch` with `credentials: "same-origin"` for any same-origin
  POST (CSRF token rides in the form data)
- Keep all error handling silent — surfaces should degrade to the
  native form behavior, not flash a JS error to the user

---

## 10. Assets & cache-busting

- `logo-bg.png` is the only mark. Use the `brand_mark` macros in
  `_partials/brand_mark.html` rather than re-pasting `<img>` tags.
- Cache-busting on static assets uses the `asset_url('css/app.css')`
  Jinja helper, which appends a content-hash query string.
- User-uploaded images (profile photos, brand logos) use a manual
  cache-buster pattern: `?v={{ updated_at|short_dt|... }}`. See
  `_partials/creator_card.html` for the canonical form.

---

## 11. CSP

`app/main.py` ships a conservative CSP:

```
default-src 'self';
img-src    'self' data: <supabase-storage-origin>;
script-src 'self';
style-src  'self' 'unsafe-inline';
style-src-elem 'self';
connect-src 'self' https://api.bigdatacloud.net;
form-action 'self' https://accounts.google.com;
frame-ancestors 'none';
```

- **No external stylesheets** — everything is `'self'`.
- **`'unsafe-inline'` for style only** because templates use inline
  `style=` attributes for form inputs and dashboard tweaks.
  Inline `<script>` is forbidden.
- New connect-src additions need to be added to the CSP — see
  BigDataCloud (reverse-geocode) for the established pattern.

---

## 12. Per-role page family conventions

When adding a new role page:

### Creator
- File: `app/templates/creator/<page>.html`
- Route: `app/routes/creator.py` under `@router.get("/creator/<page>", ...)`
- Role gate: `session: SessionPayload = Depends(require_role("creator"))`
- Onboarding redirect: every gated page should check
  `if not profile.get("onboarding_completed_at"):` and redirect to
  `/onboarding/creator`
- Mobile header partial: `creator_mobile_header.html` (already in
  base.html — don't include manually)
- Tabbar: already in base.html

### Brand
- Same shape but under `app/templates/brand/` + `app/routes/brand.py`
- Role gate: `require_role("brand")`
- Onboarding redirect: `/onboarding/brand`
- Brand surfaces tend toward `.brand-page-head` + `.brand-stat-grid` /
  `.brand-quick-actions` for dashboard-y layouts

### Operator
- `app/templates/operator/` + `app/routes/operator.py`
- Role gate: `require_role("operator")`
- No onboarding redirect (operators are invite-only and pre-onboarded)
- Use the operator topbar partial via the shell — `base.html` does this

### Marketing / auth / onboarding / legal
- These use the `is-marketing` shell (no app chrome)
- For long-form text (legal), use `<div class="legal-pane">` so the
  legal.css scoping kicks in

---

## 13. Dos and don'ts when extending the system

### Do

- Reuse existing primitives (`.btn`, `.creator-card`, `.profile-details-card`,
  `.settings-group`, `.brand-empty`, `.profile-chip-dialog`)
- Reuse existing CSS variables — every new color must be derived from
  the token table, not a one-off hex
- Mobile-first: define base styles for 390px, add `@media (min-width:
  ...)` for larger
- Add new sections to `app.css` with a header comment like
  `/* ───── section name ───── */` so the file stays navigable
- Honor `prefers-reduced-motion` in any new animation
- Include `_partials/csrf.html` in every `<form>` (CSRF middleware
  refuses unsafe-method requests without it)

### Don't

- Don't introduce a new stylesheet. Extend `app.css` (or `landing.css` /
  `legal.css` if the surface is scoped that way).
- Don't introduce a UI framework. No React, no Vue, no Tailwind, no
  Bootstrap. The system is server-rendered HTML + scoped CSS + vanilla
  JS by design.
- Don't introduce a new font family. `--sans` and `--mono` are the
  whole set.
- Don't introduce a new bottom-tab destination on creator without
  explicit approval — the 5-tab structure is canonical.
- Don't render private user data (operator notes, `verification_notes`,
  `baseline_followers`, `writing_samples`, `dm_preference`,
  `babyg_*`, `deal_*`, lat/lng) in any template. The public projections
  in `app/services/profiles.py` are the source of truth.
- Don't ship loud "fraud" or "scam" copy in creator-facing surfaces.
  Use the careful-language vocabulary documented in the brand-trust
  service.
- Don't auto-send anything. Every external action goes through
  `action_proposals` and requires explicit user confirmation.
- Don't fake stats, fake messages, fake verified states, or fake
  badges. Empty states are honest.
- Don't override `.app-shell` / `.app-main` / `.app-topbar` /
  `.app-sidebar` / `.app-tabbar` from a page-specific template. The
  shell is owned by `base.html` + `app.css`.
- Don't introduce inline `<script>`. CSP blocks it. Add a `.js` file
  in `app/static/js/` and include via `{% block head_extra %}`.

---

## 14. Quick checklist when shipping a new page

1. Does it extend `base.html`? Does it use the right role shell?
2. Does the route gate on the correct `require_role(...)`?
3. Does it redirect uncompleted onboarding to the right wizard?
4. Does every `<form>` include `_partials/csrf.html`?
5. Does it reuse existing classes (`.btn`, `.creator-card`,
   `.settings-group`)?
6. Does it look right at 390px? At 768px? At 1280px?
7. Are tap targets ≥44px on mobile?
8. Does it work with JS disabled?
9. Are private user fields excluded from the public projection?
10. Are empty states honest (no fake data)?
11. Do new colors come from the CSS variable table?
12. Did you add a section comment in `app.css` if you added rules?

---

## 15. Documentation health

If you reorganize the tabbar, add a new shell, retire a stylesheet,
or change a token: **update this doc in the same PR**. A doc that
disagrees with the code is worse than no doc.

Related operational docs:

- `docs/AUTH_DELIVERABILITY.md` — magic-link / Resend / DNS runbook
- `docs/INTEGRATIONS.md` — Google + Instagram OAuth wiring
- `AUDIT.md`, `DEPLOY.md`, `PHASE2.md` — phase-specific notes
- `README.md` — top-level project orientation
