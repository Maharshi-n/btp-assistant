# RAION Dark-First UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle RAION dark-first — pure-black canvas, glassy frosted cards, soft electric-blue glows, pill-shaped controls — across all 13 templates by rewriting the centralized `<style>` block in `base.html`, with zero changes to HTML structure or JS.

**Architecture:** All visual change lives in `app/web/templates/base.html`. A CSS-variable palette under `[data-theme="dark"]` plus targeted overrides remap the Tailwind utility classes already in the templates (`bg-white`, `bg-blue-600`, `rounded-xl`, inputs, `bg-gray-900` sidebar, etc.) to the new aesthetic. No markup, IDs, classes, or JS hooks are touched, so all functionality is preserved. The pre-paint inline script default flips from `light` to `dark`.

**Tech Stack:** Jinja2 templates, Tailwind (CDN/offline), vanilla CSS (custom properties, `backdrop-filter`), no build step.

---

## Verification model

There are no unit tests for visual CSS. Each task's "test" is a **manual visual check**: run the app, open a page, confirm the described appearance and that the feature still works. Start the server once:

```bash
cd "E:/BTP project" && python run.py   # serves http://localhost:8000
```

Login with the admin credentials from `.env`. After each task, hard-refresh (Ctrl+Shift+R) the relevant page. Keep dark mode active (it becomes the default after Task 7; before that, toggle via the sidebar "More" → theme button).

**Baseline safety:** the entire change is in one file. If anything looks broken, `git checkout app/web/templates/base.html` restores the last committed good state.

---

## File Structure

- **Modify:** `app/web/templates/base.html` — the only file changed. Its `<head>` inline theme script (line ~9-16) and `<style>` block (line ~17-170) are rewritten/extended.

All other templates are unchanged; they inherit the new look through the classes they already use.

---

### Task 1: Establish the CSS-variable palette

**Files:**
- Modify: `app/web/templates/base.html` (inside `<style>`, top of the dark-theme section ~line 18)

- [ ] **Step 1: Add the dark palette variables**

At the very start of the `<style>` block (right after `<style>`), add a `:root`/dark variable layer. Insert this block before the existing `/* ── Dark theme overrides ── */` comment:

```css
/* ── Design tokens ──────────────────────────────────────────────────────── */
:root {
  --raion-radius: 0.85rem;
  --raion-pill: 9999px;
  --raion-accent: #3b82f6;
  --raion-accent-glow: #60a5fa;
}
[data-theme="dark"] {
  --bg-canvas:   #0a0a0c;   /* app background */
  --bg-raised:   #111114;   /* sidebar / solid panels */
  --glass-bg:    rgba(28, 28, 34, 0.66);  /* frosted cards */
  --glass-bdr:   rgba(255, 255, 255, 0.08);
  --glass-shadow: 0 8px 30px rgba(0, 0, 0, 0.55);
  --txt-hi:      #f5f6fa;
  --txt-mid:     #c7cbd4;
  --txt-lo:      #8b8f9a;
  --accent:      #3b82f6;
  --accent-soft: rgba(59, 130, 246, 0.16);
  --accent-glow: 0 0 0 3px rgba(59, 130, 246, 0.18), 0 0 18px rgba(59, 130, 246, 0.28);
}
```

- [ ] **Step 2: Point the existing body rule at the canvas variable**

Find (around line 21):

```css
[data-theme="dark"] body                  { background-color: #0f172a; color: #e2e8f0; }
```

Replace with:

```css
[data-theme="dark"] body {
  background-color: var(--bg-canvas) !important;
  color: var(--txt-mid);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
```

- [ ] **Step 3: Verify**

Run the app, switch to dark mode, open the chat page. Expected: background is near-black (#0a0a0c), noticeably darker than the old slate-navy. Text is crisp/antialiased. Nothing else should look broken yet.

- [ ] **Step 4: Commit**

```bash
git add app/web/templates/base.html
git commit -m "feat(ui): add dark-theme CSS-variable palette and pure-black canvas

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Frosted glass cards

**Files:**
- Modify: `app/web/templates/base.html` (replace the `.bg-white` dark rule, ~line 22)

- [ ] **Step 1: Replace the flat card rule with glass**

Find (around line 22):

```css
[data-theme="dark"] .bg-white             { background-color: #1e293b !important; }
```

Replace with:

```css
[data-theme="dark"] .bg-white {
  background-color: var(--glass-bg) !important;
  backdrop-filter: blur(14px) saturate(120%);
  -webkit-backdrop-filter: blur(14px) saturate(120%);
  border-color: var(--glass-bdr) !important;
  box-shadow: var(--glass-shadow);
}
```

- [ ] **Step 2: Keep the message-pane assistant bubbles consistent**

Find the existing assistant-bubble rule (around line 63):

```css
[data-theme="dark"] #messages-pane .bg-white {
  background-color: #1e293b !important;
  border-color: #334155 !important;
  color: #e2e8f0 !important;
}
```

Replace with:

```css
[data-theme="dark"] #messages-pane .bg-white {
  background-color: var(--glass-bg) !important;
  border-color: var(--glass-bdr) !important;
  color: var(--txt-hi) !important;
}
```

- [ ] **Step 3: Verify**

Refresh chat, settings, and databases pages in dark mode. Expected: white cards/panels are now translucent dark "glass" with a faint light border, soft shadow, and the black canvas subtly visible through blur. Text inside remains readable.

- [ ] **Step 4: Commit**

```bash
git add app/web/templates/base.html
git commit -m "feat(ui): frosted glass cards in dark mode

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Electric-blue buttons with glow + pill shape

**Files:**
- Modify: `app/web/templates/base.html` (add new rules near the form-control section ~line 49)

- [ ] **Step 1: Add primary-button glow rules**

Add these rules inside the `<style>` block (place them just before the `/* Form controls */` comment, ~line 49):

```css
/* Primary (accent) buttons — electric blue with soft glow */
[data-theme="dark"] .bg-blue-600 {
  background-color: var(--accent) !important;
  box-shadow: 0 2px 10px rgba(59, 130, 246, 0.35);
  transition: box-shadow .18s ease, background-color .18s ease, transform .12s ease;
}
[data-theme="dark"] .bg-blue-600:hover,
[data-theme="dark"] .hover\:bg-blue-700:hover {
  background-color: var(--accent-glow) !important;
  box-shadow: 0 0 0 3px rgba(59,130,246,0.20), 0 4px 20px rgba(59,130,246,0.50);
}
```

- [ ] **Step 2: Make primary buttons and inputs pill-rounded**

Add immediately after the rules from Step 1:

```css
/* Pill-shaped controls */
[data-theme="dark"] button.rounded-xl,
[data-theme="dark"] button.rounded-lg,
[data-theme="dark"] .bg-blue-600,
[data-theme="dark"] #msg-input,
[data-theme="dark"] #send-btn,
[data-theme="dark"] #attach-btn,
[data-theme="dark"] #stop-btn {
  border-radius: var(--raion-pill) !important;
}
```

- [ ] **Step 3: Verify**

Refresh chat in dark mode. Expected: the blue "Sign in"/send/primary buttons are pill-shaped and have a soft blue glow that intensifies on hover. The message input and paperclip/stop buttons are pill-rounded. Clicking send still works.

- [ ] **Step 4: Commit**

```bash
git add app/web/templates/base.html
git commit -m "feat(ui): electric-blue glowing pill buttons in dark mode

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: Glass inputs with glowing focus ring

**Files:**
- Modify: `app/web/templates/base.html` (replace the form-control dark rules ~line 50-60)

- [ ] **Step 1: Replace the input/textarea/select dark rule**

Find (around line 50):

```css
[data-theme="dark"] input,
[data-theme="dark"] textarea,
[data-theme="dark"] select {
  background-color: #1e293b !important;
  border-color: #475569 !important;
  color: #e2e8f0 !important;
}
```

Replace with:

```css
[data-theme="dark"] input,
[data-theme="dark"] textarea,
[data-theme="dark"] select {
  background-color: rgba(255,255,255,0.04) !important;
  border-color: var(--glass-bdr) !important;
  color: var(--txt-hi) !important;
  transition: border-color .18s ease, box-shadow .18s ease, background-color .18s ease;
}
[data-theme="dark"] input:focus,
[data-theme="dark"] textarea:focus,
[data-theme="dark"] select:focus {
  outline: none !important;
  border-color: var(--accent) !important;
  box-shadow: var(--accent-glow) !important;
  background-color: rgba(255,255,255,0.06) !important;
}
```

- [ ] **Step 2: Verify**

Refresh login and chat. Expected: text fields are subtle translucent glass; clicking into one produces a soft blue glow ring instead of the old hard blue box. Typing works; placeholder text is still legible.

- [ ] **Step 3: Commit**

```bash
git add app/web/templates/base.html
git commit -m "feat(ui): glass inputs with glowing focus ring in dark mode

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: Deep-black sidebar with edge sheen

**Files:**
- Modify: `app/web/templates/base.html` (add sidebar rules; sidebar uses `bg-gray-900`)

- [ ] **Step 1: Add sidebar dark rules**

The sidebar is `<aside id="sidebar" class="... bg-gray-900 ...">` and its menus use `bg-gray-800`. Add these rules to the `<style>` block (after the Backgrounds section, ~line 28):

```css
/* Sidebar — deep black with faint right-edge sheen */
[data-theme="dark"] #sidebar {
  background-color: var(--bg-raised) !important;
  border-right: 1px solid var(--glass-bdr);
  box-shadow: inset -1px 0 0 rgba(255,255,255,0.03);
}
[data-theme="dark"] #sidebar .bg-gray-800,
[data-theme="dark"] #nav-menu {
  background-color: rgba(20,20,24,0.96) !important;
  border-color: var(--glass-bdr) !important;
}
[data-theme="dark"] #sidebar .bg-gray-700 { background-color: rgba(255,255,255,0.06) !important; }
[data-theme="dark"] #sidebar .hover\:bg-gray-700:hover,
[data-theme="dark"] #sidebar .hover\:bg-gray-600:hover { background-color: rgba(255,255,255,0.10) !important; }
[data-theme="dark"] #sidebar .border-gray-700 { border-color: var(--glass-bdr) !important; }
```

- [ ] **Step 2: Verify**

Refresh chat in dark mode. Expected: sidebar is deep black (matching the raised tone, slightly lighter than the canvas) with a barely-there light edge on its right. "New Chat", thread items, and the "More" popup menu remain clickable with subtle hover highlights. Note: the sidebar is intentionally always dark even in light mode (per CLAUDE.md) — light mode is unaffected by these `[data-theme="dark"]` rules.

- [ ] **Step 3: Commit**

```bash
git add app/web/templates/base.html
git commit -m "feat(ui): deep-black sidebar with edge sheen in dark mode

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: Global smoothness layer (transitions, scrollbars, fade-in)

**Files:**
- Modify: `app/web/templates/base.html` (replace the scrollbar rules ~line 166-169, add transition + fade rules)

- [ ] **Step 1: Smoother, slightly wider custom scrollbars**

Find (around line 166):

```css
[data-theme="dark"] ::-webkit-scrollbar             { width: 6px; height: 6px; }
[data-theme="dark"] ::-webkit-scrollbar-track       { background: #0f172a; }
[data-theme="dark"] ::-webkit-scrollbar-thumb       { background: #334155; border-radius: 3px; }
[data-theme="dark"] ::-webkit-scrollbar-thumb:hover { background: #475569; }
```

Replace with:

```css
[data-theme="dark"] ::-webkit-scrollbar             { width: 8px; height: 8px; }
[data-theme="dark"] ::-webkit-scrollbar-track       { background: transparent; }
[data-theme="dark"] ::-webkit-scrollbar-thumb       { background: rgba(255,255,255,0.12); border-radius: 9999px; }
[data-theme="dark"] ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.22); }
```

- [ ] **Step 2: Add a global transition + message fade-in**

Add at the end of the `<style>` block (just before `</style>`, after the existing `@keyframes raion-bounce`):

```css
/* Smoothness: gentle transitions on interactive elements */
[data-theme="dark"] a,
[data-theme="dark"] button {
  transition: background-color .18s ease, color .18s ease, border-color .18s ease, box-shadow .18s ease;
}
/* Subtle entrance fade for chat messages and cards */
@keyframes raion-fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}
[data-theme="dark"] #messages-pane > div { animation: raion-fade-in .22s ease both; }
```

- [ ] **Step 3: Verify**

Refresh chat in dark mode. Expected: scrollbar is a thin rounded translucent bar over transparent track. Hovering links/buttons fades smoothly. New messages gently fade/slide in. Scrolling and sending still work.

- [ ] **Step 4: Commit**

```bash
git add app/web/templates/base.html
git commit -m "feat(ui): global smoothness layer — transitions, scrollbars, fade-in

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 7: Flip default theme to dark

**Files:**
- Modify: `app/web/templates/base.html` (inline pre-paint script, ~line 11-15)

- [ ] **Step 1: Change the default theme**

Find (around line 12):

```javascript
const t = localStorage.getItem('theme') || 'light';
```

Replace with:

```javascript
const t = localStorage.getItem('theme') || 'dark';
```

- [ ] **Step 2: Verify**

In a browser with no `theme` key in localStorage (use a private/incognito window, or run `localStorage.removeItem('theme')` in devtools then hard-refresh). Expected: app loads directly in the new dark theme with no flash of light. The sidebar theme toggle still flips to light mode and persists across reloads.

- [ ] **Step 3: Commit**

```bash
git add app/web/templates/base.html
git commit -m "feat(ui): make dark the default theme

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 8: Full-page sweep + touch-ups

**Files:**
- Modify: `app/web/templates/base.html` (only if a page reveals an issue)

- [ ] **Step 1: Walk every page in dark mode**

With dark mode active, open each route and confirm cards/buttons/inputs follow the new look and remain functional:

- `/` (chat) — sidebar, messages, composer, agents drawer
- `/login` — glass card on black (log out to see it)
- `/settings`
- `/connectors`
- `/databases`
- `/automations`
- `/memory`
- `/skills`
- `/telegram-commands`
- `/whatsapp`
- `/audit`
- `/tasks`

- [ ] **Step 2: Fix any stray element**

If a specific element looks wrong (e.g. a panel that didn't pick up glass, a hard-edged button), add a targeted `[data-theme="dark"]` rule in `base.html` using the existing variables (`--glass-bg`, `--accent`, `--raion-pill`, etc.). Keep fixes centralized in `base.html`; do not edit page templates. Note any element that genuinely needs page-level markup change and report it rather than restructuring.

- [ ] **Step 3: Confirm light mode still works**

Toggle to light mode. Expected: every page renders in the original light theme (the `[data-theme="dark"]` rules do not apply), proving light mode is intact.

- [ ] **Step 4: Commit (only if touch-ups were made)**

```bash
git add app/web/templates/base.html
git commit -m "fix(ui): dark-mode touch-ups across pages

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Pure-black canvas → Task 1 ✓
- Glassy frosted cards → Task 2 ✓
- Soft colored glows → Tasks 3 (buttons), 4 (input focus) ✓
- Pill-shaped controls → Task 3 ✓
- Refined electric-blue accent → Tasks 1 (var), 3 (buttons), 4 (focus) ✓
- Centralized design system in base.html → all tasks edit only base.html ✓
- Default flips to dark, light still works → Task 7 (flip), Task 8 Step 3 (light intact) ✓
- All 13 pages covered, functionality preserved → Task 8 sweep; no HTML/JS edits anywhere ✓
- Login glass treatment → Task 8 Step 1 (and inherits from Task 2) ✓

**Placeholder scan:** No TBD/TODO. Every CSS step shows the exact CSS. Verification steps describe exact expected appearance.

**Type/name consistency:** Variable names (`--bg-canvas`, `--glass-bg`, `--glass-bdr`, `--accent`, `--accent-glow`, `--raion-pill`) are defined in Task 1 and reused consistently in Tasks 2-8.
