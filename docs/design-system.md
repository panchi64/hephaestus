# Hephaestus Design System — **Instrument**

*A quiet, high-precision interface language: Swiss neo-grotesque structure, pure-neutral surfaces, and a monospaced second voice reserved for the machine.*

**Version 1.2** · Status: Foundation spec · Supersedes *Terminal-HUD 0.1*

*1.1 adds the mission-control register: a console group, a caution & warning grid, a mission clock, the `plate` type step, and §11.1's licensed moment — one expressive device per screen, where nothing competes.*

*1.2 settles the chat surfaces on that register — **the square console**: ruled frames, bare contents, mono plates, square corners, no resting shadow. A soft-card register (rounded fills, sans labels, soft elevation) was weighed against it and rejected. It adds the **action tier** (§10.18), the one loud frame for anything the run is waiting on the operator for, and removes the LED edge light, the composer's ambient bloom and the ⌘K palette.*

---

## 0. Reading this document

This is a foundation spec, not a component catalogue. It defines the *rules* and the *tokens* — the atomic decisions everything else inherits from. Someone should be able to open an empty CSS file and reproduce the look from the values below without guessing.

Two surface modes are defined throughout: **Ink** (dark, default) and **Paper** (light). They share structure, grid, type, and motion — only color and elevation differ. Build a component once; it works in both by swapping the token set.

If this document and the code disagree, this document is wrong and should be fixed. It leads the implementation in `frontend/src/ui/theme/`, and nowhere else declares a raw value.

### What changed from Terminal-HUD 0.1, and why

The previous system was a faithful terminal/FUI pastiche: everything monospaced, everything uppercase, every corner square, no shadows, no easing. It was distinctive and it was *loud* — the density that made a cockpit readout legible made a 19-surface workspace feel like clutter, because every element shouted at the same volume and nothing could recede.

Instrument keeps the skeleton and changes the voice:

| | Terminal-HUD 0.1 | Instrument 1.0 |
|---|---|---|
| Type | Mono everywhere | **Sans for the interface, mono for the machine** (§2) |
| Labels | `UPPERCASE` + tracking, always | Sentence case; uppercase mono reserved for telemetry |
| Color | Green-tinted near-blacks, off-whites | **Pure `#000` / pure `#FFF`**, true neutral grays |
| Accent | Phosphor green, both modes | Green in Ink, **instrument blue in Paper** — and rationed harder |
| Elevation | Forbidden (no shadows) | **Subtle shadow**, plus an accent shadow for primary focus |
| Corners | Square, always | **Square, everywhere** (1.2 console register; the 3px/6px radii of 1.0 are retired) |
| Borders | "Free ink" — everywhere | **The exception** (§7); space and surface value separate first |
| Motion | Hard-cut steps only | **Two registers** — eased for the interface, instant for the machine (§8) |

What deliberately survived, because it was the good part: the 4px grid, the hairline data grid, tabular figures, semantic-only color, registration marks, and diegetic microcopy.

---

## 1. Design principles

1. **Volume is hierarchy.** The old system's failure was that everything was equally loud. Most of the interface should sit at a *low* volume — recessive, neutral, unhurried — so the one thing that matters can be the only thing that is bright, elevated, or colored. If two things on a screen are both shouting, one of them is wrong.
2. **Two voices, never blurred.** The interface speaks in sans. The machine speaks in mono. This is a semantic distinction, not a decorative one (§2).
3. **Precision without density theater.** Numbers align, fields don't reflow, state is always visible rather than implied. But density is no longer a goal in itself — whitespace is a legitimate tool, and an empty region is not wasted ink.
4. **Semantic color only.** The resting palette is pure neutral. Color appears to *mean* something — nominal, warning, alert, info, queued — or to mark the single element holding primary focus. A screen with nothing happening is grayscale.
5. **Systematic, not bespoke.** Spacing, naming, iconography, and microcopy follow consistent rules, so a screen the operator has never seen still reads as familiar.
6. **Not a generic AI app.** The signature devices — the hairline grid, registration marks, diegetic telemetry, the mono/sans split, hard-cut machine motion — are what keep this from resolving into another rounded-card chat interface. Restraint is the goal; anonymity is not.

---

## 2. The two voices

**This is the central idea of the system.** Everything else is in service of it.

### Sans — the interface speaking to the operator

`Helvetica Neue`. Everything the *product* says: page titles, field labels, body copy, button text, menu items, prose, empty states, explanations, errors written for a human.

Sans is what the operator's attention lands on. It is calm, sentence-cased, generously spaced, and it animates smoothly (§8).

### Mono — the machine showing its work

`JetBrains Mono`. Everything the *system* emits: identifiers, hashes, timestamps, durations, token counts, model names, file paths, shell output, code, diffs, coordinates, run states, latency figures, version strings.

Mono is deliberately **used sparingly and sized down**. It is ambient — the texture of a machine calculating in the background, not something to read first. It is often dim, often uppercase, always tabular, and it **never animates smoothly** (§8): mono state changes are instantaneous, because a computer does not ease.

### The test

> Would a person have written this sentence, or did a process emit this value?

Person → sans. Process → mono. A model *name* (`qwen3-30b-a3b`) is emitted by a process: mono. The word "Model" labelling it is the interface talking: sans.

When genuinely ambiguous, choose sans — the failure mode of too much mono is the cluttered terminal look this system exists to leave behind.

```
┌──────────────────────────────────────────┐
│  Active model                     ← sans │
│  Qwen3 30B A3B                    ← sans │
│  QWEN3-30B-A3B-Q4 · 18.2 GB       ← mono │
│                                          │
│  Context used                     ← sans │
│  48%                              ← sans │
│  63,104 / 131,072 TOK             ← mono │
└──────────────────────────────────────────┘
```

---

## 3. Grid & spacing

### Base unit

**4px.** All spacing, sizing, and positioning are multiples of 4. This is unchanged and non-negotiable — it is what makes the layout feel engineered rather than arranged.

### Spacing scale

| Token | px | Typical use |
|---|---|---|
| `space-1` | 4 | Label-to-value gaps, intra-control padding |
| `space-2` | 8 | Padding inside dense cells, icon-to-text |
| `space-3` | 12 | Control padding, group separation |
| `space-4` | 16 | Panel padding (default) |
| `space-5` | 20 | Panel padding, roomy |
| `space-6` | 24 | Section separation |
| `space-8` | 32 | Major region separation |

Flush is the absence of spacing and needs no token. (`space-0` and `space-12` were declared and never used, and are gone.)

### Density

Instrument runs at **one step less dense** than Terminal-HUD. Where the old system reached for `space-1`/`space-2` inside a panel, reach for `space-2`/`space-3`. Where a page used `space-4` between sections, use `space-6`.

The exception is **tabular data** — tables, instrument bands, log output, list rows. Density is a virtue there because scanning many rows is the task. Keep those at `space-1`/`space-2` vertical.

**Console groups (§10.14) and the annunciator grid (§10.15) join that exception**, for the same reason and not for a new one: their contents are ruled rows and fixed cells read by scanning, so the rule that governs a table governs them. What the exception does *not* license is a console group wrapped around prose, a form, or a stack of cards — those are read one at a time, and the frame would just be a box drawn around ordinary content at the wrong density.

### Margins & gutters

- **Gutters:** `space-4` default; `space-2` inside dense tabular regions.
- **Viewport margins:** `space-6` minimum, `space-8` on wide layouts.
- **Reading measure:** prose caps at ~72ch. Nothing forces the operator to track a line across a 2560px display.

---

## 4. Typography

### Typefaces

| Role | Stack | Notes |
|---|---|---|
| **Sans** | `"Helvetica Neue", Helvetica, Arial, system-ui, sans-serif` | The interface voice. Not self-hosted — Helvetica Neue is not licensable for web serving, and it is present on the target machine. Arial/Liberation are metric-compatible enough that the fallback degrades gracefully rather than reflowing. |
| **Mono** | `"JetBrains Mono", ui-monospace, SFMono-Regular, monospace` | The machine voice. Self-hosted woff2, subset (see `fonts.css`), including Braille Patterns for throbbers. |
| **Display** | *= Sans* | There is no separate display face. Oswald is retired: a heavy condensed grotesque was the loudest thing on any screen it appeared on, which §1.1 forbids. Display size is Helvetica at 40px with negative tracking. |

### Two scales: chrome and reading

There are **two anchors**, and keeping them apart is the point.

The **interface scale** (`--type-*`) dresses chrome — rails, labels, readouts,
tables, controls — and stays dense at a 13px body, macOS system-text density.
That is the right register for an instrument panel you *scan*.

The **reading scale** (`--prose-*`) dresses rendered markdown and nothing else —
assistant messages, research reports, documents — and is anchored on 16px/1.5,
the browser default and the size long-form reading is standardized around.

One scale cannot serve both. Pushing chrome up to 16px bloats a nineteen-row rail
into something you scroll; holding prose at 13px asks the operator to read a
five-paragraph answer at rail-label size. The surfaces differ in *how they are
looked at*, so they differ in scale.

#### Interface scale

| Token | Size/LH | Family | Weight | Case | Use |
|---|---|---|---|---|---|
| `micro` | 10 / 14 | Mono | 400 | as-is | Ambient telemetry, fine print, precision coordinates |
| `plate` | 10 / 14 | Mono | 500 | UPPER, +0.16em | **Engraved panel label.** Names a region: a console group's header, an annunciator cell |
| `meta` | 11 / 16 | Mono | 500 | UPPER, +0.08em | Machine labels & state: `RUN-0341`, `LIVE`, `QUEUED` |
| `label` | 12 / 16 | Sans | 500 | Sentence | Field labels, column headers, section eyebrows |
| `code` | 12 / 16 | Mono | 400 | as-is | Code, diffs, patches inside chrome |
| `body` | 13 / 20 | Sans | 400 | Sentence | Default interface text |
| `readout` | 20 / 28 | Sans | 500 | Sentence | Primary values, panel figures |
| `readout-lg` | 32 / 40 | Sans | 500 | Sentence | Hero value — one per screen, tabular figures |
| `display` | 40 / 44 | Sans | 600 | Sentence | Page/section titles, −0.02em tracking |

#### Reading scale (`.heph-prose` only)

| Token | Size/LH | Family | Weight | Use |
|---|---|---|---|---|
| `prose-code` | 14 / 24 | Mono | 400 | Inline and fenced code |
| `prose` | 16 / 24 | Sans | 400 | Paragraphs — 1.5, the WCAG 1.4.12 reading floor |
| `prose-h4` | 16 / 24 | Sans | 600 | `h4`; `h5`/`h6` share it at `text` rather than `text-bright` |
| `prose-h3` | 20 / 28 | Sans | 600 | `h3` |
| `prose-h2` | 24 / 32 | Sans | 600 | `h2`, −0.02em tracking |
| `prose-h1` | 28 / 36 | Sans | 600 | `h1`, −0.02em tracking |

**Four heading levels need four steps.** Rendered markdown used to put `h3` *and*
`h4` at body size, so a heading was separated from its own paragraph by weight
alone — which in a long assistant answer reads as a bolded sentence, not as
structure. `h5`/`h6` are in the ladder too, because markdown emits them and an
unstyled `h6` falls to the browser default of ~10.7px in the one place the
operator does not write the source.

**Prose code runs one step under the sans beside it.** JetBrains Mono's x-height
is large enough that a size-matched inline `code` reads as *bigger* than the
sentence around it; 14/16 is the ~0.875em every prose stylesheet on the web
converges on. Its line-height stays on the prose 24px so an inline span never
opens up the line it sits in.

**`Markdown dense` (`.heph-prose-dense`) steps headings down one rung** (`h1` at
`prose-h2`, `h2` at `prose-h3`, `h3` at `prose-h4`) and leaves body text at the
reading size. It is for prose read inside a narrow pane, where full-size headings
outshout the pane's own name; the reading floor is not negotiable, so only the
headings give.

**Never reach for `--type-*` inside `.heph-prose`, or `--prose-*` outside it.**
That is precisely what re-couples the two scales, and the next time one moves the
other follows silently.

#### Reading rhythm, and the second place it is written

Blocks sit 16px apart; a heading takes more above it and less below (`space-8` for
`h1`, `space-6` for `h2`/`h3`, `space-5` below that, `space-2` after any of them),
so a section opens on space and binds to the paragraph it introduces.

**That cascade is transcribed a second time in `Markdown.tsx`, and the copy is load-
bearing.** The `streamStable` path wraps each top-level block in its own `div`, so
those wrappers — not the `<p>`/`<h*>` elements — are what `.heph-prose`'s child
selectors match, and every heading-aware rule misses them. `blockSpacingClass` exists
to restate the same ladder in spacing utilities. When the two disagree the symptom is
nearly invisible: an answer is set to one rhythm while it streams and to another once
it reloads from history. Change one side, change the other.

#### The reading register has its own colour

`.heph-prose` takes `--text-reading`, not `--text`. The argument is the same one the
reading *scale* makes, carried into colour: a value tuned for a rail row is not a
value for five paragraphs.

**The two modes step differently, and the asymmetry is the point.** Ink moves a long
way — 8.3:1 to 13.9:1 — because light-on-dark is where the difference bites. Thin
strokes on black bleed into the ground, and a grey that reads as crisp on a one-line
label reads as haze over a screenful. Paper moves barely at all, to 12.5:1, because
halation is a property of light on dark and dark-on-light has none: 10.9:1 is
already comfortable for long-form, and driving it toward black would make a
screenful harsher rather than easier.

This leaves every other dim surface alone, which is what it is for. The deliberate
subtlety of reasoning, rails and metadata lives in `--text-dim` and the `micro`/
`meta` voices; none of it was ever wired through the prose colour, so raising the
reading register costs the ambiance nothing.

### Rules

- **Labels are sentence case.** "Context used", not "CONTEXT USED". Uppercase is now a *signal* (it means "machine"), and a signal used everywhere signals nothing.
- **Uppercase belongs to `meta`, `micro` and `plate` only**, always in mono, usually dimmed.
- **`plate` names a region; `meta` reports a state.** That is the whole distinction, and it is the §2 test applied one level down — both are the machine talking, but one is the legend engraved on a panel and the other is the value the panel is currently showing. They are deliberately one step apart in size and twice apart in tracking (+0.16em against +0.08em), because a legend that measures the same as its own readout stops reading as a legend. A third uppercase-mono step is only worth its keep while that holds: if `plate` ever starts appearing where a value should be, delete it and use `meta`.
- **Display type takes negative tracking** (−0.02em), as do prose `h1`/`h2`. Helvetica at those sizes set at 0 looks loose; this is the single most recognizable Swiss-modernist tell.
- **Tabular figures for anything that changes or aligns.** Set globally; never turn it off in a table or a counter.
- **Left-align, ragged right.** No justification. Centering only for an isolated hero readout.
- Hierarchy is **size → weight → brightness**, in that order. Reach for color last, and usually not at all.
- **A control's height is derived from its type, not chosen.** Line-height plus the padding the size is meant to read as (`Button` sm 24 / md 32 / lg 40 over the interface scale). If a step ever moves, its control heights move with it — a box sized against the old scale clips the new text.

---

## 5. Color

The base is pure neutral. Both modes are built on true black and true white — no green tint, no warm paper, no off-white. The old tints read as a *theme*; pure neutrals read as a *material*.

### 5.1 Ink (dark — default)

| Token | Hex | Use |
|---|---|---|
| `bg` | `#000000` | App background — pure black |
| `surface` | `#0A0A0A` | Panels, cards |
| `surface-raised` | `#161616` | Hover, selected, nested surfaces |
| `surface-sunken` | `#0F0F0F` | A fill *set into* the page, not lifted off it — one speaker's turn in a transcript. Nowhere below black to go, so it steps up. **Matched to Paper perceptually, not by hex delta:** the same raw step off `#000` carries far less perceived lightness than off `#FFF`, so a mirrored value read visibly fainter than its Paper twin. ΔL\* ≈ 4.3 from `bg`, against Paper's ≈ 4.2 |
| `line` | `#212121` | Hairline borders (the default rule) |
| `line-strong` | `#333333` | Emphasized borders, active control outlines |
| `text-dim` | `#6E6E6E` | Ambient telemetry, inactive labels (3.9:1) |
| `text` | `#A8A8A8` | Default text (8.3:1) |
| `text-bright` | `#FFFFFF` | Primary values, active state — pure white |

| Accent | Hex | Meaning |
|---|---|---|
| `accent` | `#34D67F` | **Signature / primary focus.** Phosphor green, retuned off the CRT glow |
| `accent-nominal` | `#34D67F` | Active, healthy, OK |
| `accent-warn` | `#F2A93B` | Caution, degraded |
| `accent-alert` | `#FF5C5C` | Error, critical, destructive |
| `accent-info` | `#5AA2FF` | Live data, secondary signal |
| `accent-queue` | `#B48CFF` | Waiting in line for capacity — a run that has not started |

### 5.2 Paper (light)

| Token | Hex | Use |
|---|---|---|
| `bg` | `#FFFFFF` | App background — pure white |
| `surface` | `#FFFFFF` | Panels sit *level* with the page; a hairline and a shadow separate them, not a fill (§6) |
| `surface-raised` | `#F5F5F4` | Hover, selected, nested surfaces |
| `surface-sunken` | `#F4F3F0` | **The one place Paper cannot hold "panels sit level with the page."** A transcript alternates two voices and tells them apart by fill; with `surface` = `bg` = white, that distinction vanishes. So it steps *down* — but only to a tint. Push it further (`#F0EFEC` was tried) and it crosses into a visible panel behind every operator turn, which is the thing this must not become |
| `line` | `#E4E4E1` | Hairline borders |
| `line-strong` | `#CFCFCB` | Emphasized borders |
| `text-dim` | `#8A8A85` | Ambient telemetry (3.5:1) |
| `text` | `#3D3D3A` | Default text (10.9:1) |
| `text-bright` | `#000000` | Primary values — pure black |

| Accent | Hex | Meaning |
|---|---|---|
| `accent` | `#0B3D91` | **Signature / primary focus.** Deep instrument blue (10.0:1 on white) |
| `accent-nominal` | `#0E7A46` | Active, healthy, OK |
| `accent-warn` | `#9A6510` | Caution, degraded |
| `accent-alert` | `#C0342B` | Error, critical, destructive |
| `accent-info` | `#0F5FA8` | Live data, secondary signal |
| `accent-queue` | `#6A3FB5` | Waiting in line for capacity — a run that has not started |

> **The signature accent is mode-dependent and that is intentional.** Phosphor green is the product's lineage and it belongs on black — it is also, literally, what a mission-control console displayed, which is why Ink keeps it. On white it turns acidic and illegible, so Paper carries the same idea executed for a light substrate: a deep instrument blue, cool and precise. `accent-nominal` stays green in both modes because green *means* "OK" independently of the theme.
>
> Paper's blue was `#0077B6` (cerulean, 4.9:1). It moved to `#0B3D91` — the darker, less saturated blue of a printed standards manual, which reads as ink on a plate rather than as a web link, and clears the §12 floor twice over at 10.0:1. **Only the signature moved.** `accent-alert` stays `#C0342B`: the brighter red of the same lineage (`#FC3D21`) measures 3.6:1 on white and fails §12 outright, and a semantic token is not worth re-pointing for a hue that cannot be read.

> **These five hexes are defaults, not constants.** The operator can set any of them, per theme, from Settings → Appearance (`ui/theme/accent-overrides.ts`, which overrides the raw `--accent*` custom properties through one scoped stylesheet — so every utility, `shadow-accent`, and the action tier's band and tint follow without knowing the feature exists).
>
> **What that does and does not relax.** The *set* of accents is closed and the meaning of each is fixed — rules 1–5 below are untouched, because they govern the token, not the hue. An operator choosing a different red for `accent-alert` has restyled "error"; they have not made `accent-alert` available for decoration.
>
> **§12's 4.5:1 floor stops being a guarantee and becomes a warning.** The shipped values are tuned to pass and a test asserts they still do (`ui/theme/contrast.test.ts`), but a chosen value is the operator's call: the editor reports the ratio and says when it is below the floor, and then honours it. Blocking would be the wrong trade — it is their interface, and a contrast ratio is information, not a permission.

### Usage rules

1. **A screen at rest is grayscale.** If nothing is running, failing, or awaiting the operator, no hue appears.
2. **One meaning per accent, system-wide.** `accent-warn` is never a brand color, a chart series, or a highlight.
3. **Brightness separates active from inactive**, not hue (`text-bright` vs `text` vs `text-dim`).
4. **At most two accents visible in a region.** Three reads as decoration.
5. **`accent` marks primary focus, and there is at most one per screen** — the primary action, the live run, the awaiting-approval card. It is the only place an accent-tinted shadow is permitted (§6).

---

## 6. Elevation & shadow

Terminal-HUD banned shadows outright. Instrument uses them, sparingly, and for one purpose: **saying what is on top.** Never for mood, never for a card that is merely present.

**Shadows are for things that float over content** — menus, popovers, tooltips, toasts, modals, drawers. Nothing that sits in the flow of a page carries one: not the composer, not a transcript row, not the action tier (§10.18). The composer is separated from the transcript by its opaque `surface` fill and its `line-strong` frame, and the transcript ends above it in the `ScrollFade` band (§10.17). `Panel` and `Tile` carry no resting shadow either: they are a `surface` fill inside a 1px `line` rule, and a state recolours the rule (`active` → `info`, `alert` → `alert`) rather than glowing.

The two modes model elevation differently, because they physically must:

- **Paper elevates with shadow.** Surfaces are pure white and level with the background, so a hairline plus a soft shadow is the only thing that lifts them.
- **Ink elevates with surface value.** A black shadow on a black background is invisible. Depth in Ink comes from `surface` → `surface-raised` and the hairline; shadow is added only for true overlays, where it darkens the content beneath.

| Token | Ink | Paper | Use |
|---|---|---|---|
| `shadow-1` | `0 0 0 1px white@5%, 0 1px 2px rgba(0,0,0,.60)` | `0 0 0 1px black@7%, 0 1px 2px rgba(0,0,0,.05), 0 1px 1px rgba(0,0,0,.04)` | Small floating things: popovers, tooltips, toasts. (Still the resting elevation of `Panel`/`Tile` on document pages.) |
| `shadow-2` | `0 0 0 1px white@7%, 0 8px 32px rgba(0,0,0,.80)` | `0 0 0 1px black@8%, 0 12px 32px -8px rgba(0,0,0,.16), 0 2px 6px rgba(0,0,0,.06)` | Overlays: modals, drawers, menus, lightbox |
| `shadow-focus` | soft white halo, `0 0 8px 1px @22%` + `0 0 22px 5px @11%` | same shape, dark | **Keyboard focus.** A halo, never an outline |
| `shadow-accent` | `0 2px 12px -2px accent@55%, 0 10px 36px -10px accent@38%` | same formula | Semantic emphasis on a panel: a live run, a blocking approval |
| `shadow-alert` | same shape, `accent-alert` | same formula | A panel whose state is genuinely wrong |

### No zero-blur layers, anywhere

**A shadow layer with no blur is a border.** `0 0 0 1px <color>` does not read as light coming off an element; it reads as an outline drawn around it, and against smoothed corners it reads as a cutout. Every attention shadow in this system — `shadow-focus`, `shadow-accent`, `shadow-alert` — is built from blurred layers only.

The single exception is the **1px hairline ring** carried inside `shadow-1`/`shadow-2` at 5–8% opacity. That is not an attention signal; it is the surface's edge definition, folded into the shadow so it costs no layout (§7). In Paper it is load-bearing: a white card on a white page has no other edge.

### Attention is drawn by luminance and mass, not glow

Hierarchy comes from brightness, and hue is reserved for meaning (§1.3, §5). The **primary button carries no halo**: an inverted bright slab is already the brightest thing in its region, and that is the whole signal.

**The composer has no glow.** It used to wear a wide neutral aura (`shadow-bloom`) on the launchpad and a lit strip along its top edge in a conversation (§10.9, removed). Both were light standing in for structure, and the console register has the structure: an opaque fill, a `line-strong` frame. Light that reaches 90px past an edge lands on the last thing the model said.

**Things waiting on the operator get mass, not light** — the action tier's filled band, tinted body and filled primary (§10.18).

### Rules

1. **Keyboard focus is a neutral halo, never an outline** — and **text-entry fields get nothing at all.** `input` and `textarea` announce focus with a blinking caret, an unambiguous native indicator no other control has; a shadow on top of it is decoration around the thing the operator is already looking at. Everything else (button, link, select, summary, anything tabbable) keeps the halo, and it must stay *visible*: a focused button has no caret, and a keyboard user who cannot see where they are is the accessibility failure this rule exists to prevent. **The one exception is a row inside a ruled list** (the action tier's answer rows, its action cells): a halo there is clipped by the neighbouring rows, so they take a 2px outline in the frame's tone instead.
2. **Shadows are for floating things only** — menus, popovers, tooltips, toasts, modals, drawers. Nothing in the flow takes one; the composer and the action tier included.
3. **`shadow-accent` is for semantic state, not for attention.** It marks a panel that is genuinely *doing* something — a live run, an approval that is blocking. It is not the composer's and not the primary button's.
4. **No shadow on a data grid.** Tables, instrument bands, and list rows sit flat. Shadows separate *layers*, and rows are not layers.
5. **Shadows never replace a surface's hairline.** Every elevated surface keeps the ring folded into its shadow so it stays defined against any backdrop.

---

## 7. Corners & borders

Corners are **square.** The 1.0 system rounded controls to 3px and containers to 6px; the console register (1.2) resolved that contradiction in favour of the machined edge, and the two radius tokens are gone. Nothing takes a radius: buttons, inputs, chips, checkboxes, tabs, menus, panels, modals, popovers, toasts, rows, cells, state marks (square, not round dots) and count badges. Square is the absence of a radius, so it has no token. Outside icon glyphs (§9), the one curve left in the product is the scrollbar thumb.

**Selection and hover are fills, not shapes.** Hover is the `surface-raised` fill and nothing else — no outline box, no radius, no accent. A selected row is `surface-raised` plus a 2px bright inset edge on its leading side (a tab: under it); a selected segmented cell is inverted. The edge is an inset shadow, so it costs no layout.

### Borders — the exception, not the default

Terminal-HUD called the hairline "free ink" and used it everywhere. It is not free. A border is a line the eye must resolve, and a screen of bordered panels inside a bordered layout beside a bordered rail is the specific thing that made the old interface feel cluttered: **every region announced its own edge, whether or not anything needed dividing there.**

The order of preference for separating two things:

1. **Space.** If a gap says "these are different", that is the whole job. Most separation is this.
2. **Surface value.** A panel one step lighter than the page is an object without being a box.
3. **The shadow's own ring.** `shadow-1` and `shadow-2` carry a 1px hairline ring as part of the shadow, so an elevated surface stays defined without a border in layout — this is what lets panels, tiles, popovers, modals, and toasts drop their borders and still read as distinct.
4. **A real border — last.** Only where a line does work nothing else can.

**A border is justified when:**

- it is a cell edge in a **ruled data grid** — a table, an instrument band, a genuinely tabular list, where the rule aligns values across rows and is doing structural work;
- it is a **control's own edge**, where the border *is* the affordance (a secondary button, a text field's resting state);
- it is a **process timeline** rail, where an unbroken vertical line is the thing being communicated;
- it is the **boundary between two tiled panes** — a splitter, or a strip stacked on the region below it. Panes fill one surface edge to edge, so there is no gap for space to work in, and anything inside a framed region is bare (§8), so there is no fill for surface value to work with. Both of the other devices are unavailable by construction, which is exactly the case this last resort is for. The splitter's own hairline is that line; it does not draw a second one;
- it is a **console group** (§10.14) — a frame with a header band, holding flush ruled rows. This is the ruled-data-grid case with a lid on it: the rows inside already carry justified rules, and the frame closes them into one readout instead of leaving a grid floating on the page with nothing saying where it starts or what it is called. Space cannot do that job here, because the rows are flush by construction and a gap between them would destroy the alignment that makes them scannable; surface value cannot either, because the cells need to read as set *into* the panel rather than lifted off it.

**A border is not justified** under a page title, under a tab strip, around every panel, under every list row, between a panel's header and its body, around a chip, or down the side of a nav rail. All of those were borders drawn where the eye had already found the break.

- **Hairline (1px, `line`)** for the justified cases above.
- **`line-strong`** marks an emphasized or hovered edge.
- **2px emphasis borders are deprecated.** Selection is now `surface-raised` + `text-bright` + `shadow-focus`. A 2px border shifts layout by a pixel on state change and reads heavy against smoothed corners.

---

## 8. Motion — the two registers

Motion follows the two voices (§2), and this is the most distinctive rule in the system.

### Human register — sans surfaces

Anything the interface does on the operator's behalf moves **smoothly**: panels, menus, drawers, modals, toasts, hovers, focus rings, disclosure, tab changes, color transitions on sans elements.

```
--motion-fast:  120ms
--motion-base:  180ms
--ease:         cubic-bezier(0.2, 0, 0, 1)   /* decelerate */
```

Short, decelerating, never bouncing. The feel is *refined and settled* — a well-damped mechanism, not a spring. Nothing exceeds 240ms; nothing overshoots.

### Machine register — mono elements

Anything rendered in mono changes **instantly**. No transition, no fade, no interpolation.

```
--motion-machine: 0ms
--ease-machine:   steps(1, end)
```

A token counter ticking, a run state flipping `QUEUED` → `RUNNING`, a latency figure updating, a log line appending, a hash resolving — these snap. The operator should feel the computer acting at machine speed while the interface around it moves at human speed.

**This contrast is the effect.** A mono value that eases into place reads as decoration and undercuts the premise; a sans panel that hard-cuts reads as broken. Match the register to the voice.

### The reveal — how anything in the human voice arrives

One motion, one component (`Reveal`), used for everything the interface puts on screen on the operator's behalf: an overlay opening, a panel revealing, a turn landing in the transcript, a run of answer text arriving.

**It is a blur fade.** Content resolves out of a blur as it fades, and with `rise`, settles the last few pixels into place while it does — so it reads as *materializing into position* rather than as a light being turned up on something that was already sitting there. That distinction is most of the refinement; opacity alone is the generic version of this.

Three parameters, no variants: `distance` (0 for a fade, 4px for an overlay settling, 10px for a turn arriving — beyond that the movement starts reading as decoration), `duration` (180ms default, 240ms ceiling), and `blur` (3px default; `0` opts a very large surface out, where blurring the whole raster costs more than the effect returns).

Two things it deliberately does not do. **It never exits** — an exit animation delays the operator getting what they asked for. **It does not replay on update** — the animation fires on mount, so a streaming block does not re-animate on every delta.

**It must never reach the machine's voice.** The per-character reveal in the answer skips code, samples, **tables** and math outright: a code block or a data grid landing hard *inside* an answer that eases in around it is the two registers visible in a single paragraph, and blurring it in would erase the one contrast the transcript is built on.

A table earns that exclusion three times over — its header band is already mono because a column header names a machine field, its cells are values a process emitted rather than sentences a person wrote (§2), and a grid is read in two dimensions, so a reveal sweeping left-to-right through cells reads as flicker rather than as arrival.

### The construction reveal — how a region the operator opens arrives

`Reveal` says *content materialized here*. Some regions want to say *a place was
made, and then filled*, and that is a different sentence: a region the operator
deliberately opened should read as having been built for them, not as having been
there all along with the light turned up.

So a **construction reveal** draws its own frame before it fills. A single `+` at
the origin corner splits; one half travels the top edge with a hairline drawn
between them; both drop down the sides as the sides close; and the glass surface
resolves inside the frame that has just been described. Closing runs the gesture
in reverse, so the region is taken apart rather than switched off.

Six surfaces use it, and the list is meant to stay short: the chat **View**,
the **settings dialog**, the **park dock** (the approval/question dock that takes
the composer's slot), the **404** plate, and the two panels behind the
composer's status bar — the **context breakdown** (behind the context bar) and the
conversation's **stats panel** (behind `stats`). Both of those go through one
shell, `RevealPopover`, so the frame, the glass and the padding inside it cannot
drift between them. It is the arrival of a place, so
it belongs to things that are places. A menu, a toast, a confirm and a form
dialog all keep the ordinary reveal — being *built* would be theatre at that
size, and the gesture stops meaning anything if everything performs it.

The two composer panels are the ones that look like popovers, and what puts them on
the list is what they're *for*, not their size. The operator opens one to read at their
own pace — what the context window is full of, and which of those answers calls for
which action; what the thread has spent and what each figure means — and points at
it while they do. That is a place they went to.
A menu is the opposite act: a choice made on the way to something else, dismissed
the moment it's made. So the composer's level picker and model picker, which share
the bar with the context gauge, stay menus. Two things side by side arriving differently is the
distinction doing its job, not an inconsistency.

It runs at the **stage budget (320ms)**, which is not a new exception to the
ceiling below: the ceiling governs what the operator is waiting on, and a whole
region arriving or leaving already has that budget. Its phases overlap; strictly
sequential beats inside 320ms read as a stutter rather than as one continuous
gesture. The content inside is **one fade, never a per-child stagger**.

The frame is **inset**, not flush to the region's edge. Flush, it lands a hairline
away from whatever edge was already there, and two rules a few pixels apart read
as a mistake rather than as a frame. Where a neighbour's edge collides, the fix
belongs to the neighbour — a splitter beside a self-framing panel hides its own
rule until reached for.

**The frosted surface is the framed region itself**, never a card wrapped around
it. Give the fill a radius and an elevation and it becomes a second container:
the frosted area then reads as a pane the marks are decorating rather than as the
pane the marks describe. Anything inside a framed region is therefore bare — one
glass layer, at the frame's own box.

And be honest about what the blur buys. Over a flat ground it does nothing,
because blurring a uniform field returns that field; translucency and the value
shift are what make it read as a material. It pays where something is genuinely
behind it — an overlay over the transcript, a dialog over a working page.

### Permitted animations

- Eased (human): the reveal above, hover and focus transitions, height on disclosure, ambient state colour (240ms).
- **The trace-in** (`.heph-trace-*`) — a row arriving on a trunk. The construction reveal's sentence at the size of one line: the elbow hanging the row off its parent draws itself down and then across (overlapping, so it reads as one L), and the row resolves out of the ordinary blur-fade as the stroke reaches it. The stroke arrives **charged**: the elbow, its length of trunk and the hard-cornered socket around the row's glyph land in the info tone and cool to their own colour over 240ms, and one band of light crosses the row's text from the socket outward — every segment clips the same band, so it reads as one sweep across the row, not one per segment. No phase runs past 240ms, the whole gesture is over by ~420ms, and nothing waits on it. The row's label speaks `meta` at the dim tone — it recedes under the header's reason, the line meant to be read first. The socket and the label are the whole work log's voice: open, the log is drawn as a schematic — one trunk down from the header's chevron, an elbow into each row's socket, a script's calls branching off the script's own socket, the chevron at the row's far end. A row the chassis put there rather than the model (injected context, a review) hangs off a **dashed** elbow into a dashed socket, which is what tells the two apart now that no row has a card to refuse; and the running row's length of trunk and its elbow turn `info`. **It plays on arrival and nowhere else.** A row traces in when its id is new to the trunk: a work log's row that a streaming run just added, and an agent thread the latest read of the thread list put under its parent in the rail. A row that only moves — a settled call leaving the pinned run for the fold or the collapsed log's latest slot — is not an arrival and stays still, as does everything on a thread opening, a log folding open, or a list re-read that added nothing. Lighting a row that had merely moved is what put the glow on the previous call instead of the new one.
- Stepped (machine): caret blink, the braille throbber (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`) as a "working now" indicator, live-state pulse, the action tier's waiting square (`steps(2)`, 1.2s; static under reduced motion, §10.18), value tick-over, **the reasoning wall** (§10.10).

The chat transcript is where the two registers are visible at once, and it is the reference implementation: the mono reasoning wall lands hard, the sans answer beneath it eases in token by token.

### Forbidden

Bouncing, springs, overshoot, staggered cascades, eased *decorative* spinners, parallax, anything over 240ms, and any animation that runs while the operator is not looking at it.

**One stated exception to the 240ms ceiling: the answer's per-character reveal (320ms).** The ceiling governs anything the operator is *waiting on* — a control answering a click, an overlay opening — where a long duration reads as lag. Nothing waits on this one: it is paced to a stream that already takes seconds, and a character is legible well before it finishes settling. Under the ceiling the resolve is over before the eye reaches it. If a second case ever wants this exemption, it has to clear the same bar — nothing is waiting.

**And the reveal's stagger is paced to the arrival rate, not fixed.** A fixed per-character step cannot serve both ends of the range: fast enough for 300 characters a second and it is invisible at 30; slow enough to see at 30 and each run is still resolving when the next three have landed, which puts later characters ahead of earlier ones and scrambles the order outright. Each run is instead spread over roughly the measured gap until the next one, so the reveal front travels at whatever speed the model is producing and looks the same at any of them. A burst too large to stagger inside that gap resolves its overflow together, which bounds how far the reveal can trail the text — the caret sits at the true end, and a reveal that lagged would strand it ahead of anything visible.

### Reduced motion

`prefers-reduced-motion: reduce` collapses the human register to 0ms. The machine register is already 0ms, so it is unaffected — the throbber is the one exception and is retained as a functional live indicator.

---

## 9. Iconography

- **Stroke-based, 1.5px visual weight**, geometric, never friendly or filled. Two source grids (16px bespoke, 24px Iconoir) are normalized to a uniform stroke by the `Icon` primitive, which also supplies round caps and joins.
- **Corners are smoothed, not square.** A rect-based glyph carries `rx="1"` on the 16px grid so it matches the rounded joins of the Iconoir set. Glyphs are the one exception to §7's square corners. Hard corners survive only where the shape *is* a hard corner — a registration mark, a crosshair.
- **Every glyph fills ~75% of its box.** That is where the Iconoir set sits; a bespoke glyph drawn edge-to-edge reads a full size step larger than its neighbours even though both render at 16px. Optical size, not nominal size, is what the eye compares.
- **Colour comes from `currentColor`**, so an icon takes the tone of the text beside it and re-colours with the theme. Never hardcode a fill or stroke colour.
- **The glyph names the action, not the metaphor everyone else uses.** A paper plane for send and an upload tray for attach are the two most generic marks in messaging UI — the exact look this system is trying not to have, and both were also the busiest shapes in the set at 16px. Send is a **departure arrow** (a diagonal, three straight lines); attaching a document is the **`file` body carrying a `plus`**, because attaching is not uploading — `upload`/`download` keep the arrow-and-tray, and they mean the server.
- **Redraw a pair, or neither.** `upload`/`download` are read as mirrors of each other; changing one alone is how a set starts to drift.
- No emoji, ever.
- **Registration marks are retained** — corner `+` crosshairs, reticles, diamond nodes — and they are one of the system's signatures. Use them at `text-dim` and at `micro` scale so they frame without competing.
- **A registration mark must bracket an object, not the window.** They belong on `PageHeader`, framing the plate that holds the asset id, title, subtitle and status badge; on a hero card; on a lightbox. They do **not** go on the shell's content region — that put two crosses just above the page and two in the bottom corners of the screen, framing the viewport rather than anything in it, which is the one job a registration mark has. Whatever they bracket needs enough inset (~20px) that the marks never collide with the content.
- Icons inherit `currentColor` and follow the text tone they sit beside.

---

## 10. Components

Anatomy plus tokens. These map directly onto `frontend/src/ui/`.

### 10.1 Field — label + value

The atomic unit. A sans `label` (sentence case, `text-dim`) above or beside a `body`/`readout` value (`text-bright`). An optional `meta` line beneath carries the machine detail.

```
Active model                  ← label,   sans 12, dim
Qwen3 30B A3B                 ← readout, sans 20, bright
QWEN3-30B-A3B-Q4 · 18.2 GB    ← meta,    mono 11, dim, uppercase
```

### 10.2 Panel

`surface` fill, square, `space-4` padding, inside a 1px `line` rule. No shadow. Optional header: a `plate` left, meta/status right. A state recolours the rule (`active` info, `alert` alert). Selected state lifts to `surface-raised`.

`Composer` takes a `bare` prop too, but it means something narrower: **it drops the fill and keeps the frame.** For a composer sitting inside another surface (nested in a `Panel`, the approval dock), a fill on a fill is the box-in-a-box §7 exists to stop, and it reads as a grey slab. The `line-strong` frame is what still says "type here".

A `bare` panel also keeps its **state** shadow (`active`/`alert`) while dropping the resting `shadow-1`. `shadow-1` exists to define a surface and there isn't one; a live or failing state is semantics, and a panel that drops its fill shouldn't go silent about being live.

**`bare` drops the surface entirely** — no fill, no shadow, no ring, no card padding. The panel becomes its label and its content, sitting directly on the page.

This is for regions that should read as *behind* the interface rather than as objects on it: the home page's recent-threads and in-flight lists, the system strip. They are things the operator glances past on the way to the composer, and giving each one a card turned the launchpad into a wall of boxes competing with the single surface that matters. Structure still comes from the label and the spacing — it just stops being a container.

The rule generalizes: **a card is a claim on attention.** Anything ambient — telemetry, recent items, background activity — should be bare, so the operator can look past it to what is live.

**A region whose width the operator controls lays out by container query, not viewport breakpoint.** A resizable pane (the chat viewport's surfaces) can be 320px or 1200px on the same screen, so `sm:`/`md:` answer a question about the window that the content never asked. The pane is the container (`@container`, named where its header needs its own query); what sits in it responds with `@md:`/`@3xl:`. Viewport breakpoints stay for the page shell, whose width *is* the window's.

### 10.3 Instrument band

Full-width strip of densely packed machine fields, hairline-separated, square, `space-2` padding, mono throughout. This is where density stays aggressive and where mono is at its densest — it is explicitly the machine's own readout.

### 10.4 Readout

The hero value: `readout-lg`, sans, `text-bright`, tabular, with a dim sans `label`. One per screen. Its supporting machine detail goes in a `meta` line, not in the readout itself.

### 10.5 Status flag

A small `meta` label — uppercase mono, no fill, no border. This is machine state, so it snaps between values with no transition.

**The dot carries the hue; the label stays dim.** A status flag reports, it does not ask. A whole word in an accent colour reads as a demand for attention wherever it appears, and a screen with a dozen flags then makes a dozen demands — which is how an accent budget (§5.4) gets spent on nothing.

The exception is `warn` and `alert`, which keep the accent on the label too. Those are the two states that genuinely need to interrupt, and a failure the operator can miss is worse than one more colour on the page. That exception is what makes the rule worth having: when only real problems are coloured, a coloured word means something.

### 10.6 Tile / nav card

`surface`, square, a 1px `line` rule, a geometric glyph and a sans `label`. Hover is the `surface-raised` fill. Selected adds `text-bright` and the 2px inset edge.

### 10.7 List row

Single line, square, hairline beneath, sans label left, mono meta right. Hover fills `surface-raised`. Disabled drops to `text-dim` with a lock glyph.

### 10.8 Controls (button, input, select, chip, toggle)

Square, hairline border, `space-2`/`space-3` padding, sans label. Focus is `shadow-focus`, always neutral. A **primary** button — one per view — is the sole carrier of `shadow-accent`.

### 10.9 Live edge — a coloured rule, not light

*The LED edge light (`LedEdge`, `.heph-led*`) is removed.* It was a hairline with a four-layer directional glow reaching ~90px past it; the console register says the same thing without light.

A region that is running right now says so by **its rule turning `info`**: the running row's length of trunk and its elbow in the work log, the reasoning wall's left edge while it streams, a rail row's leading edge (in its run state's own tone, and only for the states worth marking from across the room — live work, a failure, a limit). The rule is the same 1px in both states, so marking a region shifts nothing, and the colour change is ambient state, so it eases (§8). Nothing glows, nothing spills onto its neighbours, and no scroll container has to keep padding for light to fall into.

### 10.10 Reasoning stream

The clearest expression of the two voices in the product, and the reference for how any "the machine is working" surface should behave.

While the model reasons, its trace is **not a message**. It is the computer working *behind* the response area:

- **Mono, `micro` size, barely tinted** — `accent` mixed at ~25% into `text-dim`, held at ~30% opacity. It reads as machine texture, not as content, and never competes with the answer.
- **A flat wall, not an effect.** The subtlety lives entirely in the *color*: one uniform value across the whole block. **No gradient and no mask.** A faded edge would make it a decorated panel; this is meant to be a surface — a dense wall of machine text sitting behind the response.
- **Clipped into the background** of the response area, behind the content, `pointer-events: none`.
- **The stage travels; it does not appear.** The wall's height is a token (`--reasoning-h`), and the region opens and closes over that distance in `--motion-stage` rather than popping in and out at it — a region this large arriving instantly is a jump the reader absorbs twice per turn. Both directions are one token each, so retiming or resizing the state change is a token edit; nothing holds a matching copy, and the component waits on `animationend` rather than a duration of its own.
- **Cascading and bottom-anchored**, so new tokens push older lines up and out of frame. The wall stays filled to its top edge with older reasoning, and the newest line sits at the bottom where the eye already is.
- **Fixed-height stage**, so the transcript does not reflow line-by-line while the trace streams.
- **Machine register (§8)** — tokens land hard as they arrive. No easing, no fade-in per token.
- **Nothing sits in the foreground.** No label, no throbber. Text arriving *is* the signal that the model is working — the most direct one available — and the `info` edge beside it already says so. Both a "THINKING" label and a spinner on top were restating what the wall and the rail were already communicating, which is the clutter this system exists to remove.

**The handoff.** When the turn resolves, the wall **fades to the background over ~320ms** in the human register — that is the interface clearing the stage, not a control responding — and the **collapsed reasoning accordion fades in where it stood**, above the response. Two fades in the same place, so it reads as one movement rather than as a block being swapped out. The accordion holds the full trace, still mono and dim: available, out of the way.

The live layer is `aria-hidden` (it is texture, and a token-by-token live region would be unusable); a polite live region announces that the model is reasoning, and the settled accordion carries the real content.

### 10.11 The answer stream

The counterpart to §10.10, and the other half of the same idea.

The answer is **the interface speaking**, so it belongs to the human register: **each newly arrived run of text fades in over ~220ms**, eased, while everything already on screen stays put. Only genuinely new characters animate — settled text carries no animation even as the markdown block around it re-renders — and a run whose fade is interrupted by the next delta simply lands at full opacity, so the token at the head of the stream is the one that visibly arrives.

Put the two side by side and the system explains itself without a word being read: **the reasoning wall lands hard because it is mono and the machine is thinking; the answer eases in because it is sans and the interface is replying.**

Markdown structure must survive the stream — blocks that have settled keep their DOM across deltas, so only the trailing block re-parses. A streaming answer that renders as plain text and then pops into formatting when it finishes is not acceptable.

**Links in an answer.** A link is the only element in an answer that is a *place the operator can go*, which is what earns it the one accent the resting palette spends on prose: `--accent-info`, underlined, 2px offset. An outbound link — anything but `mailto:` — carries a dim `↗` immediately after its last word, bound to it by a non-breaking space so it can never wrap alone, and names its base domain in the anchor's `title`. The pair is deliberate and split: **the glyph says the link leaves the workspace; the hover says where it goes.** The glyph is a pseudo-element, so it never animates in with the reveal above and never lands in a copied selection.

**A file in an answer.** A path the model wrote is not a place the operator can go — it is a file on their own machine — so it is not an anchor and does not pretend to be one. It is a button: mono, `--accent-info`, dotted underline, and a click asks the machine the app is running on to open that file in whatever the desktop opens it with. The split is the same one the `↗` makes: **the dotted underline says the click leaves the browser entirely; the hover says which file.** Mono because a path is machine voice (§2), which also keeps it out of the answer's reveal — it lands hard while the prose around it eases in. Which files may be opened is settled by the backend against the operator's own project roots and never in the renderer, because the path came out of a model's prose.

Below the answer, the **Sources row** lists the pages the turn's tools actually retrieved, numbered, as base-domain chips. The two devices answer different questions and both are needed: Sources is *what this turn read*, an inline link is *where this sentence came from*. They use the same domain label so the operator can tell at a glance when they are the same page.

### 10.12 The composer

**One component, one layout, two sizes.** The docked input bar and the home page's hero field are the same unit — `size` changes padding (and the hero keeps its width), and nothing else. They used to be separate branches (a bordered field nested inside a bordered bar; a 2px box), then a rounded card with a loose row of controls floating under it.

**One ruled unit, square** (the console restyle). A single `line-strong` frame on a `surface` fill — no radius, no shadow — holds, top to bottom: the **plate** (a 7px square, then mono uppercase dim: `<thread> · ready`, the branch on the right; while a run streams it turns `info`, the square blinks, the activity reads `Streaming · step n` and the run clock sits on the right; while the thread waits on the operator it is *replaced* by the action tier's amber strip, `Waiting on you · Jump ↑`), the **field** (full width, one line at rest, a line at a time up to 10 lines, then it scrolls), square mono **attachment chips** (type tag, name, ×), and the **receded strip** (`StatusBar variant="recede"`): `surface-sunken`, a hairline, no dividers, dim mono at 28px, brightening only on hover/focus-within — `+`, model, level (its coloured square), mode | `↵ new line · ⌘↵ send`, the thread's tasks/stats, a 64px context bar with its fold tick and %, and **Send** — outlined, dim while empty, bright-outlined with text, filled on hover; Stop while streaming (alert square, `STOP esc`) beside Queue. Its pickers open square ruled menus (`Popover variant="console"`: colour square, label, number key, one-line description). `/` and `@` tokens are a sigil cell welded to a raised name cell — a command's sigil solid inverse in a bright border, a file's outlined with its directory dim. The paragraphs below describe the earlier card + status-bar arrangement; where they disagree, this one is right. `bare` keeps the frame and drops the fill.

The card — `surface`, **only the writing**, top to bottom:

1. **the header line** — one line of `plate` mono (10px, engraved tracking, dim): what the input is doing on the left, what is waiting on the operator on the right. The caller fills both (`headerStart`/`headerEnd`); `title` is the left's fallback (the hero's "New conversation"). In chat: `INPUT` at rest; while a run streams `● RUN` in info (`● FOLDING` for a hand-started fold), then `T+` the run's `MetClock` from `run.started`'s own `ts`, then `STEP n` from the newest `step.started` — each only once the stream has reported it. It is also where the thread's state is said now that the title carries none: `DISCONNECTED` in alert outranks everything (a detached run's clock would go on ticking over nothing), and at rest a run state other than `DONE` — `FAILED`, `BLOCKED`, `CANCELLED`, or a run the backend reports live that this room isn't driving — replaces `INPUT` as `● STATE` in the `runState.ts` table's own status tone, the same table the rail reads. On the right, `QUEUED n · ↑ TO EDIT` in warn while operator messages wait in the run; otherwise `DRAFT SAVED` while the field holds a draft persisted under `storageKey` (only on a composer that already has a header, so the first keystroke never grows a line);
2. attachment chips;
3. one row: the field — **transparent, borderless, no focus ring of its own** — with the commit keys at its right, bottom-aligned so they stay by the last line as the field grows.

**The commit keys are the machine's.** `Button size="console"`: mono, uppercase, 11px semibold, 28px tall, the chord after the word at half opacity. At rest `TRANSMIT ⌘↵` (primary slab). While a run streams `QUEUE ⌘↵` (the same key — the message waits for the run's next step) beside `STOP ESC`, which reads `STOP & CORRECT ESC` once the field holds text, in the `danger` outline: it is the one key on the unit with a consequence the operator can't take back. While recalling, `SAVE ⌘↵`.

**The status bar** (`StatusBar`, `joined`) is a 24px ruled row of fixed cells: `line` hairlines between them, mono 11px lowercase in the `text` tone, hover raising a cell to `surface-raised`/`bright`. Left, what the message *is*: `+ file` (line-strong and inert while a run streams or a message is recalled — present, so nothing reflows), the level picker (`Select cell`, its colour square kept), the model (`Combobox cell`, capped and truncating first). Right, the state of the thread: `tasks 3/5 · active` (only when there are tasks), `stats`, and the **context bar** — `ctx`, an 80×6px track filled to the window fraction, and the figure. The figure takes the backend's `level`: text, then warn, then alert. A 1px warn tick crosses the track at this thread's fold point (`usage.fold`, the operator's threshold with the conversation's override applied) while folding is armed; with it off or unreported there is no tick. Clicking the gauge opens the context breakdown. The bar wraps at narrow widths — the end group drops to its own line still right-aligned — and draws nothing at all when both groups are empty (the compare bench, the dock's own composer).

**The card is for writing; the bar is for everything about the writing.** The controls used to sit inside the card in an action row, which made the resting card three lines tall for one line of text and put a toolbar between the operator and the field. Joined under it as a ruled strip they read as what they are — settings of the message and state of the thread — while staying part of the one object, rather than page furniture that happened to sit below it.

**One row at rest, grows with its content.** The field rests at a single text row in both sizes; the card is the header, that line and the commit key. It grows as the operator writes, up to about 40% of the viewport, then scrolls — recomputed on resize, so a short window never loses the key off its bottom edge.

**Enter is a newline; ⌘↵ (Ctrl↵ elsewhere) sends.** A multi-line field that sends on Enter punishes the operator for writing a paragraph; the commit key carries the chord as its hint, so the rule is on screen rather than remembered. Enter during IME composition is never a send.

**↑ in an empty field recalls; the header says so.** With a `recall` controller wired (chat, for its queued messages), ArrowUp on an empty field lends the field to the newest recalled message, caret at its end; ↑ at the caret's very start walks older, ↓ at its very end walks newer and steps back out past the newest. Anywhere else the arrows move the caret as always. While recalling, the header line is taken over — never stacked under — by the pen glyph and what is being edited on the left and `⌘↵ SAVE · ESC CANCEL` on the right; the key reads **Save**, attaching is off, chips and `/`/`@` accents stand down, and the entry is announced in the composer's live region. The operator's own draft is never touched — it is not persisted over and comes back the moment the recall ends. Dim rather than accented: a mode of the input is a state, not a signal (§5).

**The card is the control.** The field carries no chrome because the card around it already is the input; a bordered box inside a bordered box is two lines where one object exists.

**The composer is always marked, at rest.** It *is* the operator's point of action, so "start typing" must be the obvious move the moment the screen appears — never gated on focus or hover (§6), because a cue that only arrives once you have committed to typing is telling you something you no longer need. The mark is structure, not light: the opaque `surface` fill, and the `line-strong` frame. Neutral at rest, not accented: luminance, not hue.

**No glow, in either size.** The composer used to wear an ambient aura (`shadow-bloom`) on the launchpad and a lit strip along its top edge in a conversation (`edge="bloom" | "led"`). Both are removed, and so is the `edge` prop: the aura's ~90px of reach landed on the last thing the model said, and the frame already separates the input from the conversation (§6).

**Docked, it floats.** No rule welded to the bottom edge — the sticky wrapper carries the page background, and the transcript dissolves into it through a bottom `ScrollFade` (§10.17) reaching one band up over the conversation; the transcript keeps exactly that band as its bottom inset, so the last turn at rest clears the fade. Below the unit, only an 8px margin — the shell gives the chat route its bottom edge flush. **While the approval dock holds the slot**, the bar's trailing cells stand on their own under it as the same `StatusBar`, boxed on all four sides (`edge="box"`), with `DISCONNECTED` as its leading cell — the header line that says it the rest of the time has gone with the composer.

### 10.12a The chat room's title

The thread's name and nothing else on its side — the workspace hint under it for a staged worktree thread, the `Frames` throbber while the backend names it — with the branch chip, surface buttons and session menu at the other end. The model, the thread's age and the run state used to ride an eyebrow above it; the model is a control in the composer's status bar, and the state is said in the composer's header line. **The title carries no fill and is laid over the top of the transcript**, which scrolls beneath it into a top `ScrollFade`; the transcript's top inset is the title block's measured height plus one band, so the first turn at rest starts clear of both. A thread recap pinned under the title keeps the ground as a fill — it is a paragraph, and prose scrolling through prose is unreadable at any blur.

### 10.13 The operator’s turn

The operator's own message is **a square block anchored right, shrink-to-fit, capped at 78%** of the column.

**The block.** `surface-raised` fill, square corners, no shadow. Its edge is one **1px `line-strong` border on all four sides** — the console's ordinary ruled cell. A heavier 3px `text-bright` edge on the right was tried and dropped in favour of the plain border: the block's side of the column and its fill already say whose it is. **It carries no speaker label.** A `YOU` tag was tried in the top-right corner and removed: the row it needed left an empty band over every message. A header row appears only when the turn has state to show (queued, held, pinned, a version cycler). The body is `text-bright` at the **same size as an answer** (the reading scale — a larger operator size was tried and rejected), left-aligned, padded 10px / 14px.

**Time is a hover reveal, on every turn.** No message prints a timestamp at rest. Hovering or focusing inside a turn shows its written-out time in dim mono: on the operator's block it appears in the margin to the block's left, level with its first line; on an answer, where the date line was. It is taken out of flow (absolutely placed on the operator's block; an answer's header row is already its fixed 24px), so showing it moves nothing. The reveal is the turn controls' own (`REVEAL_ON_GROUP_HOVER`): on a device with no hover it stays shown rather than vanishing, and the full date also rides in the block's `title`. The operator block's Copy / `···` sit in the margin to the left of its foot, so they never meet the stamp.

Width follows content: a three-word prompt is a three-word bubble. A fixed-width block would leave a short turn floating in the middle of an empty region, which reads as a layout error rather than as a message. The cap is what keeps a long paste from spanning the full measure.

Alignment of the *block* and alignment of the *words* are separate decisions: the block sits right (that is what marks it as the operator's), the text inside is left-aligned, because right-ragged prose with any internal structure reads as broken.

**It is rendered, not printed.** The turn goes through `Markdown` — `.heph-prose`, the reading scale, the answer's own colour — so a prompt carrying a list, a fence or a `>` quote is set the way the answer beside it is. Printing the operator's source beside the model's rendered prose read as the product taking one speaker less seriously than the other. The block's edge and tag are what mark the turn as theirs; the text takes `text-bright` (the one colour difference from the answer). A long turn clamps with a `ScrollFade variant="clamp"` over its foot and a Show more/less toggle beneath.

**The transcript's chrome runs at the composer's density; its prose does not.** Every row that isn't reading text — a work log's header and rows, a View chip, a turn's identity row — is 24px, the height of the composer's status cells, and the work log's sockets are 16px so a schematic row is no taller than a plain one. Turns are separated by 24px (12px of padding above and below each), and the parts inside an assistant turn sit 8px apart. The reading scale is untouched by any of it: density is taken out of the furniture around the words, never out of the words.

**Turns glide in.** Every turn — the operator's on send, the assistant's as it opens — fades up 10px over 200ms in the human register. Fast enough to read as a response rather than a reveal, and it fires once on mount, so a streaming turn never re-animates as deltas land. Opening a thread mounts its turns together, so the transcript settles in as one movement rather than a staggered cascade.

One implementation note that is easy to get wrong: the entry animation runs with `animation-fill-mode: both`, and an animated `opacity` outranks a utility class. A turn that is also *dimmed* (above a compaction divider) therefore needs two nodes — one owning the dim, one owning the movement — or the animation silently cancels the dim.

### 10.14 Console group

A frame, a header band, and flush ruled rows. The header carries a `plate` label at the left and an optional readout at the right; the rows inside sit square, `space-1`/`space-2` vertical, hairline-separated, edge to edge with no padding of their own.

This is the one shell that is allowed to be a box (§7), and the reason it exists rather than being drawn per screen is that three surfaces want the identical skeleton — the launchpad's sequencer, the annunciator grid's frame, and the chat status strip. A shape repeated three times in three files drifts three ways.

**What goes in one:** ruled rows and fixed cells — things read by scanning a column. **What does not:** prose, forms, or a stack of cards. A `Panel` (§10.2) is still the default container, and it stays the default; a console group is what you reach for when the content is genuinely a readout and the frame is telling the operator where that readout begins and ends.

### 10.15 Annunciator grid — caution & warning

The capability strip as a console panel: one cell per capability, laid out as a square ruled grid with 1px `line` gaps, inside a console group.

**A cell is two lines: the legend, and why it is lit.** An unlit cell carries its `plate` legend at a tone barely above the surface it sits on, and an empty second line. A lit cell fills, and its reason occupies that second line — scrolling (`Marquee`) rather than truncating where it overflows the column, because the reason is the one thing in the cell the operator has to finish reading. The second line is reserved either way, so a capability degrading changes a cell's contents and never its size.

The reason used to be set in a list *beneath* the grid. That restated every legend in order to say the thing the cell it named had room for, and grew the panel by a line per fault.

**Caution is a tint; alert is a solid fill.** With the reason occupying the second line there is no status *word* left, so §12 has real work to do here: the two severities are told apart by fill weight — a luminance difference that survives any colour vision — rather than by hue. It is also what the hardware does, where a master alarm is not merely a different colour from a caution. Lit and unlit stay separable on a third, independent channel: an unlit cell's second line is empty.

**The column count comes from the cell count**, capped per breakpoint (4 / 6 / 8), filling the rows as evenly as possible. A fixed count left seven capabilities sitting 5 + 2, and the three dead positions read as a row that failed to finish. It cannot always reach zero — nine cells at a cap of eight is 5 + 4 whatever is chosen — so it minimizes the gap rather than promising none. Letting the last row's cells *stretch* would promise none, and is rejected: the columns would stop aligning between rows, which is the ruled structure §7 keeps the borders for.

This replaces a flat band where every cell was equally loud, and it is a better instrument for the same reason the real panel was: **the operator reads the panel by looking for the lit one.** A band of eight equally-bright cells has to be read left to right every time; a matrix with one amber cell in it has already answered the question from across the room. It is §1.1 — volume is hierarchy — applied to a region that was violating it.

Never hue alone (§12): the fill weight separates the two severities, an empty second line separates unlit from lit, and the console group's header carries the count in words.

The failure mode is real and worth naming: with few enough capabilities, a grid of dark cells can read as a wall of empty boxes rather than as a calm panel. If it does, render only the lit cells plus a single count of the rest.

### 10.16 Mission clock

`T+ 004:21:07` — elapsed time since a thing started, fixed-width, zero-padded, every field always present. Mono, `meta` or `plate` depending on where it sits, and squarely in the machine register (§8): it snaps, it never eases, and it never animates between values.

**It is a formatter over a backend timestamp and nothing else.** The frontend is given `created_at`/`started_at` and renders the difference; it does not decide when anything began. A clock is the easiest place in an interface to accidentally invent state, and this one must not.

One is in use: the **run clock** on an in-flight run, from its `started_at`, in the composer's header line. The **session clock** that ran from the conversation's `created_at` beside the chat title was removed — how long ago a thread was opened is not a question the operator reads the room to answer. A clock is diegetic detail under §11 and stays within its budget — one per region, at the region's edge.

### 10.17 Scroll fade

Where a scroll region runs **under** something laid over its edge — the chat title above, the docked composer below — it dissolves rather than being cut at a line. `ScrollFade edge="top" | "bottom"` (`.heph-scroll-fade`) fills its positioned parent and reaches one `--scroll-fade-band` (28px) past the side facing the content. Two effects on one falloff, strongest at the edge and nothing at the band's far end: a **progressive backdrop blur** — three layers, each masked to a shorter reach and blurring harder (a quarter, a half, all of `--scroll-fade-blur`), so there is no seam where one stops — and a **tint of the ground** from `--scroll-fade-tint` (70%) to clear, which keeps a title legible over text passing under it. Either alone fails: a gradient alone is a smear of crisp grey text; a blur alone leaves the title's contrast to whatever scrolls by.

The scroll region keeps an inset of exactly one band at that edge (plus the overlay's own height at the top), so what rests there at the end of the scroll clears the fade — never more, or the band becomes dead page. The overlay's content must be positioned to paint above it, and the backdrop-root trap applies in full: nothing between the scroller and the fade may set `opacity`, `filter`, `mask`, `isolation` or a `will-change` of them, and the overlay takes its paint order from the DOM rather than a `z-index`. Pointer-transparent and hidden from assistive tech.

**`variant="clamp"`** is the same idea at the scale of one clamped block — the operator's long turn, a compaction section — saying "there is more below" over its foot: a plain 24px gradient from the block's own ground (`ground="bg" | "surface" | "sunken"`), inside the block, no blur (`.heph-clamp-fade`). A hand-rolled `bg-gradient-to-t` is the thing it replaces.

### 10.18 The action tier — things that need the operator

A coloured border alone is too quiet for the one moment the run cannot continue without the operator. In the console register anything **waiting on the operator** gets *mass*: `ActionFrame` (`ui/components/ActionFrame.tsx`), with `tone` naming the kind of ask — **`approval`** (warn/amber: a sensitive action is parked), **`question`** (info/blue: the agent asked something), **`alert`** (alert/red: a conflict or stop that needs a decision). Everything else in the transcript stays bare.

**Anatomy.**

- **The band** — a solid plate in the tone's colour, full width, `space-2`/`space-3` padding: the title on the left and an optional `aside` on the right in `meta` mono, uppercase, in the **inverse** tone (`Text tone="inverse"`, the ground colour), then a 6px **blinking square** in the ground colour. The square blinks hard-stepped (`steps(2)`, 1.2s — machine register) and is static under reduced motion. `live={false}` drops it for a settled record of an ask.
- **The body** — the tone's colour mixed into the ground at ~7% (approval, alert) or ~5% (question), inside a 1px border of the tone. Square corners, **no shadow** — it sits in the flow (§6).
- **No padding imposed.** The frame does not pad its body: a question sits flush under the band, an approval pads its own command and reason.
- **`ActionButtons`** — a wrapping row of square cells with **joined borders** (each cell after the first drops its left rule): mono `meta` uppercase, `line-strong` outlines, and **one filled primary** (`text-bright` on the ground — luminance, not hue). A cell may print its key (`kbd`: `↵`, `esc`) after the label at reduced opacity; binding the key is the consumer's.
- Focus inside the tier is a **2px outline in the tone**, not the halo — a halo is clipped by the neighbouring rows (§6 rule 1).

**The echo.** An ask is visible from anywhere, not only where it was raised: the composer's plate is replaced by an amber strip ("N waiting · Jump ↑"), and the thread's rail row takes an inset bar and a `WAITING` tag. (Specified here; landing with the composer and rail restyle.)

**The question card** — `ActionFrame tone="question"` around `ChoiceList` (`ui/components/ChoiceList.tsx`), and the shape of **any** multi-choice ask:

- **The frame is the card.** No padding wrapped around the core: the question text sits directly under the band, then the answers.
- **The answers are a `radiogroup` of edge-to-edge ruled rows**, never laid out side by side. Each row: a 22px square **keycap** with its number, the answer in `body` bright, and its **consequence right-aligned on the same line** in a dim `label` (it drops under the answer below 560px).
- **The picked row** is a **filled keycap** in the tone and a row tinted ~16% toward it — not a full inversion, which would outshout the band. Hover tints ~6%.
- **Free text is a real input row** with a **dashed** keycap (solid in the tone while focused). Typing deselects the options, and the button reads **Send answer**.
- **One footer**: the key hints at the left (`1–N pick · ↑↓ move · type to answer freely`, `micro` dim) and a single filled **ANSWER ↵** at the right.
- **Keyboard**: a number key picks its row (1–9), ↑/↓ move the pick with a roving tab stop, Enter submits — from a row or from the free text.
- Presentation only: `value`/`onChange`/`freeText.onInput`/`onSubmit` — it holds no state and decides nothing about what an answer does.

`ParkDock`, `QuestionAnswerCard`, `ConflictCard` and plan approval all move onto it.

### States — all components

| State | Treatment |
|---|---|
| Default | `text` on `surface`, hairline `line` |
| Hover | `surface-raised`, eased (`motion-fast`) |
| Inactive | drop to `text-dim` |
| Selected | `surface-raised` + `text-bright` |
| Focus (keyboard) | `shadow-focus` — neutral, both modes |
| Primary focus | `shadow-accent` — one per screen |
| Alert | `accent-alert` on the value or border, never a fill |
| Loading | sans "Loading…" or a mono braille throbber — never an eased spinner |
| Empty | sans "No data" / a written sentence explaining what would appear here |

---

## 11. Diegetic detail

The sense that this is a real instrument comes mostly from *content*, not styling — and it now has a natural home, because all of it is machine output and therefore mono, dim, and small (§2). That is what lets it stay without becoming clutter.

Keep:

- **Asset & version IDs** — `RUN-0341-A7`, `IDX-v3.2.1`
- **Precision values** — coordinates, byte counts, token counts, latencies to the millisecond
- **Plausible telemetry** — uplink latency, queue depth, cache hit rate
- **Consistent naming** — pick a scheme (`[DOMAIN]-[SUBSYSTEM]-[SEQ]`) and hold to it

**The budget:** at most one diegetic detail per panel, and it lives at the panel's edge — a footer line, a header's right slot — never between a label and its value. Terminal-HUD's mistake was letting atmosphere sit in the reading path. Set at `micro`/`meta` in `text-dim`, it becomes texture the eye skips until it wants it, which is exactly the intent.

### 11.1 The licensed moment

Everything above is texture the eye skips. **One thing per screen is allowed not to be.**

A licensed moment is a single expressive device — the `DeepField`'s graticule, limb and stars are the first and currently only one — permitted on a screen where *nothing is read against it*. That is a short list, and it is the whole rule:

- an **empty state**, which by definition has nothing in it;
- the **auth screens**, which hold one form and no work;
- the **launchpad**, whose one object is a composer and whose panels are readouts;
- a **lightbox**, which has already darkened everything else.

**The field backs a page, not a region.** It was first cropped to the launchpad's composer band, and the crop is what argued against itself: the limb is drawn as a horizon too large to see the whole of, and cut off two hundred pixels either side of the composer it reads as an arc in a box. A horizon has to run the width of the frame or it is not one. So the shell paints it behind its whole content column (`isDeepFieldRoute`, `AppShell`), status bar included, and the screen stops owning it.

**What makes that safe is glass, and the two are one decision.** A page-wide field runs under content the operator is meant to read, which is the "atmosphere in the reading path" failure this section exists to prevent — unless nothing readable sits *on* the field. On a field page every framed shell (`.heph-framed`: `Panel`, `ConsoleGroup`, `Composer`, `InstrumentBand`) trades its opaque `bg-surface` for the same glass the View and the park dock use, so a graticule behind a column of numbers arrives as a blur rather than as lines through them. A shell with no surface of its own — a bare panel, the system strip — carries no marker and is instead given space, which is the same answer §7 gives everywhere else. **Ship one without the other and the rejection above stands.**

**Still rationed, and still never inside a container holding data.** Two routes carry it (the launchpad and the 404), the list is explicit rather than a default, and matching is exact — `/` is a prefix of every path in the app, and a `matchesHref` here would put the field behind all of it, which is the outcome the ration exists to prevent.

**Why the exception is worth having at all.** §1.4 says a screen at rest is grayscale and §1.1 says most of the interface should sit at a low volume. Both are right, and followed absolutely they produce a system with no register above "quiet" — which §1.6 already names as the other failure: *restraint is the goal; anonymity is not*. An interface that is only ever recessive has no way to say that the thing in front of you is the point. The moment is that register, and rationing it to one per screen, in a region with nothing to drown out, is what keeps it from becoming decoration.

**It must stay under the thing it sits behind.** The launchpad's moment sits behind the composer, the screen's point of action — two claims on the eye in the same 200px. The field is neutral and very low contrast, it picks up `--accent` only at the limb and only at low alpha, and where the two compete the field loses. The composer is the point of the screen; the field is the reason the screen feels like somewhere.

**Nothing in it is an instrument.** A reticle bracketing one of the stars was drawn and removed: it reads as the interface pointing at something, and there is nothing there to point at. Diegetic detail that *implies a readout* is a different thing from diegetic detail, and the difference is that one of them is a lie about what the product is doing.

---

## 12. Accessibility

- **Never encode meaning in hue alone.** Every accent is paired with a label, glyph, or position change — already required by §5.
- **Contrast floors:** `text` on `bg` ≥ 7:1, `text-bright` on `bg` ≥ 15:1, `text-dim` on `bg` ≥ 3:1, every accent on `bg` ≥ 4.5:1. The tokens in §5 are tuned to pass; re-verify after any hue change. The accent half of that is now automated — `ui/theme/contrast.test.ts` asserts every shipped accent clears 4.5:1 in its own mode, so retuning one and forgetting to check fails the suite. **A user-set accent (§5) is warned about, never blocked**, and the warning is judged on the *displayed* (one-decimal) ratio so the verdict and the figure beside it can never contradict each other.
- **Focus is always visible.** `shadow-focus` is neutral and high-contrast in both modes; never suppress the ring without an equally visible replacement.
- **Mono is small by design** — so it is never the only carrier of essential information. Anything the operator must read to act is sans at `body` or larger.
- **Reduced motion** collapses the human register (§8).
- Tabular figures and consistent alignment aid low-vision scanning; keep them.

---

## 13. Token summary

```css
:root {
  /* ---- grid ---- */
  --space-1: 4px;  --space-2: 8px;   --space-3: 12px;  --space-4: 16px;
  --space-5: 20px; --space-6: 24px;  --space-8: 32px;

  /* ---- corners & borders (square = no radius, no token) ---- */
  --radius-full: 9999px;  /* the scrollbar thumb, and nothing else */
  --line-w: 1px;

  /* ---- the two voices ---- */
  --font-sans: "Helvetica Neue", Helvetica, Arial, system-ui, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, "SFMono-Regular", monospace;

  /* ---- type scale ---- */
  --type-micro-size: 10px;      --type-micro-lh: 14px;
  --type-plate-size: 10px;      --type-plate-lh: 14px;
  --type-meta-size: 11px;       --type-meta-lh: 16px;
  --type-label-size: 12px;      --type-label-lh: 16px;
  --type-body-size: 13px;       --type-body-lh: 20px;
  --type-readout-size: 20px;    --type-readout-lh: 28px;
  --type-readout-lg-size: 32px; --type-readout-lg-lh: 40px;
  --type-display-size: 40px;    --type-display-lh: 44px;
  --tracking-label: 0.08em;   /* uppercase mono meta only */
  --tracking-plate: 0.16em;   /* engraved panel labels */
  --tracking-tight: -0.02em;  /* display */

  /* ---- motion: human register ---- */
  --motion-fast: 120ms;
  --motion-base: 180ms;
  --ease: cubic-bezier(0.2, 0, 0, 1);
  /* ---- motion: machine register ---- */
  --motion-machine: 0ms;
  --ease-machine: steps(1, end);

  /* ---- INK (dark, default) ---- */
  --bg: #000000;         --surface: #0a0a0a;      --surface-raised: #161616;
  --surface-sunken: #0f0f0f;
  --annunciator-dark: #0d0d0d;   /* an unlit caution & warning cell */
  --line: #212121;       --line-strong: #333333;
  --text-dim: #6e6e6e;   --text: #a8a8a8;         --text-bright: #ffffff;
  --text-reading: #d2d2d2;       /* prose only — 13.9:1 */
  --accent: #34d67f;
  --accent-nominal: #34d67f;  --accent-warn: #f2a93b;
  --accent-alert: #ff5c5c;    --accent-info: #5aa2ff;
  --accent-queue: #b48cff;
  /* Each carries a 1px hairline ring first — the surface's edge, folded into the
     shadow so it costs no layout (§6). Floating things only. */
  --shadow-1: 0 0 0 1px rgb(255 255 255 / 0.05), 0 1px 2px rgb(0 0 0 / 0.6);
  --shadow-2: 0 0 0 1px rgb(255 255 255 / 0.07), 0 8px 32px rgb(0 0 0 / 0.8);
}

[data-theme="paper"] {
  --bg: #ffffff;         --surface: #ffffff;      --surface-raised: #f5f5f4;
  --surface-sunken: #f4f3f0;
  --annunciator-dark: #f7f7f5;
  --line: #e4e4e1;       --line-strong: #cfcfcb;
  --text-dim: #8a8a85;   --text: #3d3d3a;         --text-bright: #000000;
  --text-reading: #343431;       /* prose only — 12.5:1 */
  --accent: #0b3d91;
  --accent-nominal: #0e7a46;  --accent-warn: #9a6510;
  --accent-alert: #c0342b;    --accent-info: #0f5fa8;
  --accent-queue: #6a3fb5;
  --shadow-1: 0 0 0 1px rgb(0 0 0 / 0.07), 0 1px 2px rgb(0 0 0 / 0.05),
    0 1px 1px rgb(0 0 0 / 0.04);
  --shadow-2: 0 0 0 1px rgb(0 0 0 / 0.08), 0 12px 32px -8px rgb(0 0 0 / 0.16),
    0 2px 6px rgb(0 0 0 / 0.06);
}

/* mode-invariant, derived. Blurred layers only — a zero-blur layer is a border
   (§6), and these resolve the *live* accent, so one formula covers both modes
   and follows an operator-set hue (§5.2) with no second definition. */
:root, [data-theme="paper"] {
  --shadow-focus:
    0 0 8px 1px color-mix(in oklab, var(--text-bright) 13%, transparent),
    0 0 22px 5px color-mix(in oklab, var(--text-bright) 7%, transparent);
  --shadow-accent:
    0 2px 12px -2px color-mix(in oklab, var(--accent) 55%, transparent),
    0 10px 36px -10px color-mix(in oklab, var(--accent) 38%, transparent);
  --shadow-alert:
    0 2px 12px -2px color-mix(in oklab, var(--accent-alert) 50%, transparent),
    0 10px 36px -10px color-mix(in oklab, var(--accent-alert) 34%, transparent);
}
```

---

## Appendix A — One-line brief

> A quiet, pure-neutral interface on a strict 4px grid, set in Helvetica with a monospaced second voice reserved strictly for machine output; hierarchy from size, weight, and brightness, with color rationed to semantic state and a single mode-dependent accent — phosphor green on black, instrument blue on white — marking the one thing that needs attention; separation carried by space and surface value rather than by borders, which survive only inside a ruled data grid; marginally smoothed corners; subtle elevation used only to say what is on top; and motion split into two registers, smooth and decelerating for the interface, instantaneous for anything the computer is doing.
