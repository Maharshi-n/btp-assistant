# RAION UI Dark-First Redesign — Design

**Date:** 2026-05-26
**Author:** Maharshi Nahar (with Claude)
**Status:** Approved

## Goal

Make RAION's UI dark-first, premium, smooth, and simple. Adopt the aesthetic of a
reference screenshot the user liked: pure-black canvas, glassy frosted cards, soft
electric-blue glows, and pill-shaped controls. Apply consistently across all 13
templates with **zero loss of existing functionality**.

## Decisions (locked with user)

- **Scope:** Dark-first redesign. Dark becomes the primary, polished theme across
  all pages. Light mode stays functional but secondary.
- **Visual traits:** pure-black canvas, glassy/frosted cards, soft colored glows,
  pill-shaped controls — all four.
- **Accent:** refined electric-blue (keep blue family, richer + glowing).
- **Implementation:** centralized design system in `base.html` (CSS variables +
  reuse of existing Tailwind classes), not per-page handcrafting.
- **Verification:** deliver code; user runs `python run.py` to review.

## Approach

The current look comes from Tailwind utility classes in templates (`bg-white`,
`rounded-xl`, `bg-blue-600`, ...) plus a `[data-theme="dark"]` override block in
`base.html`. Keep all template markup untouched; supercharge `base.html` so the same
classes resolve to the new aesthetic in dark mode. One file restyles all 13 pages.

### Mechanics

1. **CSS variable palette** under `:root` (light) and `[data-theme="dark"]`:
   - Canvas `#0a0a0c`, raised `#111114`, glass overlay surfaces.
   - Electric-blue accent `#3b82f6` → glowing `#60a5fa`.
   - Refined text contrast ramp.
2. **Map existing Tailwind classes** in dark mode:
   - `bg-white` → frosted glass card (translucent + `backdrop-filter: blur`, thin
     light-alpha border, soft shadow).
   - `bg-gray-900` sidebar → deep black with faint right-edge sheen.
   - `bg-blue-600` buttons → electric-blue with soft outer glow on hover/focus.
   - `rounded-xl`/`rounded-lg` softened; inputs & primary buttons become pill-rounded.
   - inputs/textarea → glass fields with glowing focus ring (no harsh blue box).
3. **Global smoothness layer:** transitions on interactive elements, smoother custom
   scrollbars, antialiased text, subtle entrance fade for messages/cards, focus glows.
4. **Default theme flips to `dark`** in the pre-paint inline script (`'light'` →
   `'dark'` default). Light mode remains a working alternative.
5. **Login page** inherits the glass-card-on-black treatment automatically (uses
   `bg-white`/`bg-gray-50`); confirm it reads well.

## Non-goals / will NOT touch

- No HTML structure changes, no renamed IDs/classes, no JS logic. All `onclick`/`id`
  hooks the JS relies on stay identical → functionality preserved.
- No new dependencies (CDN Tailwind, no build step).
- Light mode stays functional.

## Risks & mitigations

- **`!important` specificity:** extend the existing override pattern so cards/buttons
  reliably restyle. Centralized variables = one place to adjust strays.
- **`backdrop-filter` performance:** cheap for the few panels on screen; fine on
  modern browsers.
- **Readability on pure-black + glass:** keep a tested text-contrast ramp.

## Deliverable

Rewritten `base.html` `<style>` block + theme-default flip. User reviews via
`python run.py`. Per-page touch-ups are quick follow-ups if needed.
