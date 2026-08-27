# Frontend fidelity gap report

Reference: `babyg Design System (1).zip`. Functionality and data remain owned by the existing Flask/Jinja application. The public landing page is excluded.

## Creator Home

- Already matched: dark warm palette, rose accent, orchid AI color, five-tab navigation, trust vocabulary, real Hot Drops and feedback actions.
- Gap found: the page used a legacy dashboard sequence with oversized calendar/AI command cards and no reference-style daily action list or calendar preview.
- Markup: `app/templates/creator/dashboard.html` was recomposed into greeting, status, Needs you, Up next, activity stats, and Hot Drops.
- CSS: `app/static/css/app.css` now owns the compact surface cards, action rows, date strip, trust chips, and iPhone layout.
- Issue types: layout, component structure, hierarchy, spacing, state styling. Status: resolved; the calendar remains an honest entry/empty state because this route does not receive booking rows.

## Home - Needs you

- Already matched: real unread notification and DM counts existed.
- Gap found: only the first notification appeared in a loose alert strip; DMs and alerts were not expressed as actionable reference rows.
- Markup: real unread DMs and up to three real unread notifications now populate a contained module. A truthful caught-up state replaces invented tasks.
- CSS: compact icon rails, caution/neutral chips, dividers, truncation, and 44px-plus targets were added.
- Issue types: component structure, hierarchy, spacing, state styling. Status: resolved within available route data.

## Home - Calendar preview

- Already matched: calendar route and connected calendar flow were preserved.
- Gap found: Home only linked to a large command card and did not resemble the reference date-strip preview.
- Markup: Home now includes a five-cell visual strip and a contained calendar state linking to the real calendar.
- CSS: selected-day accent, compact mono labels, event/empty row treatment, and mobile fit were added.
- Issue types: layout, spacing, component structure, state styling. Status: visually resolved; event details cannot be rendered on Home without changing its route context.

## Home - Hot Drops

- Already matched: operator-published data, confidence, expiry, source, category filtering, CSRF, and feedback behavior.
- Gap found: cards were generic intel panels with a legacy header/filter hierarchy.
- Markup: cards now use reference-style toplines, confidence chips, colored rail, source footer, and compact feedback actions.
- CSS: card spacing, type scale, metadata, filter pills, and responsive footer were rebuilt.
- Issue types: hierarchy, component structure, typography, spacing, state styling. Status: resolved.

## Creator Discover

- Already matched: filters, pass, save, connect/interested, details, undo, real cards, trust notes, empty states, and swipe behavior.
- Gap found: desktop headings and filters are more expansive than the kit's compact mobile top bar; some fact grids remain data-first rather than portrait-first.
- Markup: `app/templates/creator/discover.html` already has the required functional card composition and was retained to protect its forms and bindings.
- CSS: existing scoped redesign supplies raised swipe cards, horizontal tabs, action dock, trust styling, and mobile targets.
- Issue types: minor layout and spacing. Status: close; portrait artwork cannot match static kit examples when real profiles have no image.

## Creator DMs

- Already matched: inbox list, unread state, peers, timestamps, thread navigation, private brief, composer, and send flow.
- Gap found: legacy naming and some row density remain slightly different from the React specimen.
- Markup: `app/templates/creator/dm_list.html` and `dm_thread.html` preserve real loops and semantics.
- CSS: the redesign layer provides compact list surfaces, trust/risk states, pill composer, and fixed mobile placement.
- Issue types: minor typography and spacing. Status: close.

## DM brief card

- Already matched: risk chip, summary, missing terms, deal read, reply options, follow-ups, refresh, and use-draft behavior.
- Gap found: the current production card contains more explanation than the compact reference specimen.
- Markup: `app/templates/creator/dm_thread.html` retains the full real brief contract and every bound action.
- CSS: orchid-tinted private surface, risk/safe chips, structured blocks, and mobile one-column layout are implemented.
- Issue types: density and hierarchy. Status: functionally higher-detail than the kit; use draft still only fills the composer.

## Babyg tab

- Already matched: private-manager identity, message bubbles, proposal content, server errors, composer, and real bot action.
- Gap found: the welcome hero is taller than the reference TopBar on large screens.
- Markup: `app/templates/creator/bot.html` retains the working conversation and no-JS form.
- CSS: orchid AI surfaces, compact chat frame, safe-area composer, and status treatment are implemented.
- Issue types: minor vertical spacing. Status: close.

## Profile and settings

- Already matched: public preview, creator identity, tags, integrations, privacy, Babyg behavior, bio editing, and real forms.
- Gap found: the real settings surface contains more controls and therefore runs longer than the compact kit mockup.
- Markup: `profile.html` and `profile_settings.html` retain all real fields, names, actions, and CSRF includes.
- CSS: preview card, grouped settings surfaces, toggles, inputs, and mobile tap targets use the reference system.
- Issue types: density and component quantity, not visual theme. Status: close, with intentional functional expansion.

## Brand dashboard

- Already matched: completion, four real stats, four quick actions, customer navigation, and live counts.
- Gap found: weak section hierarchy and no compact live/workspace status made it feel like a themed admin screen.
- Markup: `app/templates/brand/dashboard.html` now uses reference-style workspace heading, status, Overview, completion, and Quick actions hierarchy.
- CSS: customer-app status chip, accented completion surface, section heads, compact stat grid, and responsive actions were added.
- Issue types: hierarchy, spacing, component structure, state styling. Status: resolved.

## Brand Discover

- Already matched: the brand route reuses the real Discover card engine with creator-specific actions, filters, save, connect, details, and undo.
- Gap found: real creator cards can be text-led when profile media is absent, unlike the visual kit examples.
- Markup: shared `app/templates/creator/discover.html` remains the correct reusable implementation; `brand/discover_detail.html` owns detail behavior.
- CSS: brand scope inherits the swipe-card surfaces, action dock, trust states, and mobile filters.
- Issue types: media availability and minor layout. Status: close without fake creator imagery.

## Operator console

- Already matched: live queue counts, operational routes, recent audit data, abuse/brand/campaign/listing/member surfaces, and desktop operator shell.
- Gap found: seven equal workspace cards formed a long portal grid rather than the reference command-center snapshot and queue layout.
- Markup: `app/templates/operator/console.html` now uses a mission-control header, system state, live metrics, contained queue panel, and adjacent activity panel.
- CSS: two-column desktop composition, dense queue cards, responsive collapse, and status treatment were added.
- Issue types: layout, hierarchy, component structure, spacing, state styling. Status: resolved while preserving every operational entry point.

## Remaining intentional differences

- Reference mock data was not copied. Empty, image-less, or count-zero states reflect real application data.
- Creator Home cannot list booking event rows until its existing route provides them; it links to the fully functional calendar and does not claim a connection state it cannot verify.
- Production DMs, settings, and operator areas expose more real functionality than the sample screens, so their total page length is greater.
