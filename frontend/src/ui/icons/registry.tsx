import type { JSX } from "solid-js";

/**
 * Geometric, stroke-based icons (design system §9). Each entry returns the inner
 * SVG markup; the Icon primitive supplies the <svg> wrapper, sizing, stroke,
 * currentColor, and round caps/joins. No fills, no skeuomorphism, no emoji.
 *
 * Two rules keep a hand-authored glyph consistent with the Iconoir set:
 * - **Corners are smoothed, not square.** A rect-based glyph carries `rx="1"` on
 *   the 16px grid, so an icon's corners match the 3px/6px language of the
 *   controls and panels it sits inside (§7). Hard corners survive only where the
 *   shape *is* a hard corner — a registration mark, a crosshair.
 * - **Every glyph fills ~75% of its box.** That is where the Iconoir set sits, so
 *   a bespoke glyph drawn edge-to-edge reads a full size step larger than its
 *   neighbours even though both render at 16px.
 *
 * Two grids coexist behind one uniform look:
 * - **Bespoke HUD glyphs** (reticle, cross, diff panels, …) are hand-authored on
 *   the native **16px** grid — a bare `() => JSX`.
 * - **Iconoir-sourced glyphs** carry geometry on Iconoir's **24px** grid, wrapped
 *   in `g24(...)`. The Icon primitive normalizes stroke weight across grids so a
 *   24-grid glyph renders at the same visual 1.5px as a 16-grid one.
 *
 * To pull more Iconoir icons: add a mapping in `scripts/gen-iconoir.ts`, run it,
 * and paste the emitted `g24(...)` entries here (Iconoir is a devDependency only —
 * only the inlined markup ships).
 */
type IconGlyph = () => JSX.Element;
export type IconEntry = IconGlyph | { viewBox: number; glyph: IconGlyph };

/** Tag a glyph as living on Iconoir's 24px grid. */
const g24 = (glyph: IconGlyph): IconEntry => ({ viewBox: 24, glyph });

export type IconName =
  | "cross"
  | "reticle"
  | "chevron-right"
  | "chevron-down"
  | "chevron-up"
  | "chevron-left"
  | "arrow-right"
  | "u-turn"
  | "plus"
  | "minus"
  | "close"
  | "check"
  | "dot"
  | "filter"
  | "search"
  | "menu"
  | "warning"
  | "info"
  | "lock"
  | "key"
  | "eye"
  | "edit"
  | "trash"
  | "refresh"
  | "download"
  | "upload"
  | "attach"
  | "send"
  | "chat"
  | "play"
  | "pause"
  | "stop"
  | "settings"
  | "user"
  | "users"
  | "mail"
  | "calendar"
  | "file"
  | "note"
  | "image"
  | "database"
  | "cpu"
  | "terminal"
  | "code"
  | "activity"
  | "bell"
  | "link"
  | "plug"
  | "mic"
  | "clock"
  | "layers"
  | "grid"
  | "panel-right"
  | "archive"
  | "library"
  | "pen"
  | "inject"
  | "review"
  | "compare"
  | "branch"
  | "research"
  | "copy"
  | "pin"
  | "sun"
  | "moon"
  | "system";

export const icons: Record<IconName, IconEntry> = {
  // ── Bespoke HUD glyphs (native 16px grid) ─────────────────────────────
  cross: () => <path d="M8 3v10M3 8h10" />,
  // A registration mark, and one of the system's signatures — kept, but pulled
  // in to the shared ~75% optical box. At 1..15 it filled 87% of the frame and
  // read a full step larger than every Iconoir glyph beside it.
  reticle: () => (
    <>
      <circle cx="8" cy="8" r="4.5" />
      <path d="M8 2v1.5M8 12.5v1.5M2 8h1.5M12.5 8h1.5" />
    </>
  ),
  dot: () => <circle cx="8" cy="8" r="2" />,
  // A funnel — a list filtered by a scope, where the scope's name has no room.
  filter: () => <path d="M2.5 3.5h11L9.5 8.5v4l-3 1.5V8.5z" />,
  // A trunk with one line breaking off it — a forked conversation and a git
  // branch are the same shape, so they share the glyph.
  branch: () => (
    <>
      <circle cx="5" cy="4" r="1.6" />
      <circle cx="5" cy="13" r="1.6" />
      <circle cx="12" cy="7" r="1.6" />
      <path d="M5 5.6v5.8" />
      <path d="M5 10c4 0 7 0 7-1.4" />
    </>
  ),
  stop: () => <rect x="3.5" y="3.5" width="9" height="9" rx="1" />,
  layers: () => <path d="M8 2L2 5l6 3 6-3zM2 9l6 3 6-3M2 12l6 3 6-3" />,
  plug: () => (
    <>
      <path d="M6 2v4M10 2v4" />
      <path d="M4 6h8v2a4 4 0 0 1-8 0z" />
      <path d="M8 12v3" />
    </>
  ),
  "panel-right": () => (
    <>
      <rect x="2" y="3" width="12" height="10" rx="1" />
      <path d="M10 3v10" />
    </>
  ),
  compare: () => (
    <>
      <rect x="2" y="3" width="5" height="10" rx="1" />
      <rect x="9" y="3" width="5" height="10" rx="1" />
    </>
  ),
  research: () => (
    <>
      <circle cx="7" cy="7" r="4" />
      <path d="M10 10l4 4M7 5v4M5 7h4" />
    </>
  ),
  /* Redrawn, and moved off the 24 grid to sit beside the other glyphs, which is
     where the borrowed one gave itself away. Iconoir's bubble was a circle of r=10 with a nub: the one fully round
     form in a system whose corners are chamfered, filling 83% of its box against
     `code`'s 75%, so it read a full size step larger than the glyph next to it
     (§9). Both faults are the same fault — it was a stock mark, and a round
     speech bubble is to a chat control what the paper plane was to send.

     A bubble is a frame, and a frame in this hand carries `rx="1"`. So: one
     closed outline, chamfered at all four corners, its tail cut out of the
     bottom edge rather than stuck under it, at 75% of the box. */
  chat: () => (
    <path d="M3 3h10a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H8l-3 2.5V12H3a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" />
  ),
  pen: () => <path d="M2 14s1-3 3-5l5-5 2 2-5 5c-2 2-5 3-5 3z" />,
  // Something placed *into* a frame from outside it: an open-sided box with an arrow
  // entering. Drawn rather than borrowed because the meaning it has to carry — context
  // the chassis put in front of the model, as opposed to a call the model made — is one
  // no icon set has a word for, and the whole job of the glyph is to be unmistakably not
  // one of the tool families beside it on the rail.
  inject: () => (
    <>
      <path d="M9.5 3H12.5a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H9.5" />
      <path d="M2.5 8h5.5" />
      <path d="M6 5.5 8.5 8 6 10.5" />
    </>
  ),
  // A shield with a check inside it: something was vouched for before it happened. Drawn
  // rather than borrowed for the same reason `inject` is — the row it leads has to read
  // as neither a tool call nor an injection, and no icon set carries "the chassis ruled
  // on this in the operator's place".
  review: () => (
    <>
      <path d="M8 2.5 3.5 4.2v3.9c0 2.4 1.8 4.1 4.5 5.4 2.7-1.3 4.5-3 4.5-5.4V4.2L8 2.5Z" />
      <path d="M6 8.1 7.3 9.4 10 6.7" />
    </>
  ),

  // ── Iconoir-sourced glyphs (24px grid, via scripts/gen-iconoir.ts) ─────
  "chevron-right": g24(() => (
    <>
      <path d="m9 6l6 6l-6 6" />
    </>
  )),
  "chevron-down": g24(() => (
    <>
      <path d="m6 9l6 6l6-6" />
    </>
  )),
  "chevron-up": g24(() => (
    <>
      <path d="m6 15l6-6l6 6" />
    </>
  )),
  "chevron-left": g24(() => (
    <>
      <path d="m15 6l-6 6l6 6" />
    </>
  )),
  "arrow-right": g24(() => (
    <>
      <path d="M3 12h18m0 0l-8.5-8.5M21 12l-8.5 8.5" />
    </>
  )),
  /* A U-turn laid on its side: the line runs right, loops back around, and returns
     along the top with the head pointing the way it came. `arrow-right` on a RETURN
     control pointed the operator forward, at the one control on the screen whose
     whole job is retracing a wrong turn — and the upright U-turn that replaced it
     read as a road sign, because a vertical loop is a direction you drive, not a
     direction you go back in. Horizontal is what makes it a *return*: the axis is
     the one the reader's own eye travels. */
  "u-turn": g24(() => (
    <>
      <path d="M4.5 8H15q0 0 0 0s5 0 5 4.706C20 18 15 18 15 18H6.286" />
      <path d="M7.5 11.5L4 8l3.5-3.5" />
    </>
  )),
  plus: g24(() => (
    <>
      <path d="M6 12h6m6 0h-6m0 0V6m0 6v6" />
    </>
  )),
  minus: g24(() => (
    <>
      <path d="M6 12h12" />
    </>
  )),
  close: g24(() => (
    <>
      <path d="M6.758 17.243L12.001 12m5.243-5.243L12 12m0 0L6.758 6.757M12.001 12l5.243 5.243" />
    </>
  )),
  check: g24(() => (
    <>
      <path d="m5 13l4 4L19 7" />
    </>
  )),
  search: g24(() => (
    <>
      <path d="m17 17l4 4M3 11a8 8 0 1 0 16 0a8 8 0 0 0-16 0" />
    </>
  )),
  menu: g24(() => (
    <>
      <path d="M3 5h18M3 12h18M3 19h18" />
    </>
  )),
  warning: g24(() => (
    <>
      <g>
        <path d="M20.043 21H3.957c-1.538 0-2.5-1.664-1.734-2.997l8.043-13.988c.77-1.337 2.699-1.337 3.468 0l8.043 13.988C22.543 19.336 21.58 21 20.043 21ZM12 9v4" />
        <path d="m12 17.01l.01-.011" />
      </g>
    </>
  )),
  info: g24(() => (
    <>
      <path d="M12 11.5v5m0-8.99l.01-.011M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2S2 6.477 2 12s4.477 10 10 10" />
    </>
  )),
  lock: g24(() => (
    <>
      <path d="M16 12h1.4a.6.6 0 0 1 .6.6v6.8a.6.6 0 0 1-.6.6H6.6a.6.6 0 0 1-.6-.6v-6.8a.6.6 0 0 1 .6-.6H8m8 0V8c0-1.333-.8-4-4-4S8 6.667 8 8v4m8 0H8" />
    </>
  )),
  key: g24(() => (
    <>
      <path d="M10 12a4 4 0 1 1-8 0a4 4 0 0 1 8 0m0 0h12v3m-4-3v3" />
    </>
  )),
  eye: g24(() => (
    <>
      <g>
        <path d="M3 13c3.6-8 14.4-8 18 0" />
        <path d="M12 17a3 3 0 1 1 0-6a3 3 0 0 1 0 6" />
      </g>
    </>
  )),
  edit: g24(() => (
    <>
      <path d="m14.363 5.652l1.48-1.48a2 2 0 0 1 2.829 0l1.414 1.414a2 2 0 0 1 0 2.828l-1.48 1.48m-4.243-4.242l-9.616 9.615a2 2 0 0 0-.578 1.238l-.242 2.74a1 1 0 0 0 1.084 1.085l2.74-.242a2 2 0 0 0 1.24-.578l9.615-9.616m-4.243-4.242l4.243 4.242" />
    </>
  )),
  trash: g24(() => (
    <>
      <path d="m20 9l-1.995 11.346A2 2 0 0 1 16.035 22h-8.07a2 2 0 0 1-1.97-1.654L4 9m17-3h-5.625M3 6h5.625m0 0V4a2 2 0 0 1 2-2h2.75a2 2 0 0 1 2 2v2m-6.75 0h6.75" />
    </>
  )),
  refresh: g24(() => (
    <>
      <g>
        <path d="M21.888 13.5C21.164 18.311 17.013 22 12 22C6.477 22 2 17.523 2 12S6.477 2 12 2c4.1 0 7.625 2.468 9.168 6" />
        <path d="M17 8h4.4a.6.6 0 0 0 .6-.6V3" />
      </g>
    </>
  )),
  /* A matched pair, and they only work as one — redraw both or neither.
     Iconoir's stock version hangs the arrow over a bare floating dash, which at
     our weight reads as an underline rather than a destination. These give it a
     tray instead: a bracket with the house 0.6-unit corner (§7 — smoothed, not
     rounded), the same idiom `file` and `archive` already use. The arrowhead is
     narrowed from 7 units to 6.2 so it reads as a point rather than a wedge.
     Mind the gap between shaft and tray: stroke is 2.25 units on this grid, so
     anything under ~2.5 units of clearance closes up at 16px. */
  download: g24(() => (
    <>
      <path d="M12 3.6v9.9m-3.1-3.1L12 13.5l3.1-3.1M4.5 16v4.4a.6.6 0 0 0 .6.6h13.8a.6.6 0 0 0 .6-.6V16" />
    </>
  )),
  upload: g24(() => (
    <>
      <path d="M12 13.5V3.6M8.9 6.7L12 3.6l3.1 3.1M4.5 16v4.4a.6.6 0 0 0 .6.6h13.8a.6.6 0 0 0 .6-.6V16" />
    </>
  )),
  /* Attaching a file to a message is not uploading one. `upload` says "this
     goes to the server"; here the operator is adding a document to what they
     are about to say, and an arrow-into-a-tray reads as the former. Built on
     `file`'s exact body so it is a member of that family rather than a new
     shape, with `plus` in the page instead of the text rules. */
  attach: g24(() => (
    <>
      <g>
        <path d="M4 21.4V2.6a.6.6 0 0 1 .6-.6h11.652a.6.6 0 0 1 .424.176l3.148 3.148A.6.6 0 0 1 20 5.75V21.4a.6.6 0 0 1-.6.6H4.6a.6.6 0 0 1-.6-.6" />
        <path d="M16 2v3.4a.6.6 0 0 0 .6.6H20" />
        <path d="M12 11.5v6M9 14.5h6" />
      </g>
    </>
  )),
  /* A departure arrow, not a paper plane. The plane is the single most generic
     mark in messaging UI — the thing this system is trying not to look like —
     and its interior fold made it the busiest glyph in the set at 16px. The
     diagonal says "off it goes" with three straight lines. */
  send: g24(() => (
    <>
      <path d="M6.5 17.5L17.5 6.5M10 6.5h7.5V14" />
    </>
  )),
  play: g24(() => (
    <>
      <path d="M6.906 4.537A.6.6 0 0 0 6 5.053v13.894a.6.6 0 0 0 .906.516l11.723-6.947a.6.6 0 0 0 0-1.032z" />
    </>
  )),
  pause: g24(() => (
    <>
      <path d="M6 18.4V5.6a.6.6 0 0 1 .6-.6h2.8a.6.6 0 0 1 .6.6v12.8a.6.6 0 0 1-.6.6H6.6a.6.6 0 0 1-.6-.6Zm8 0V5.6a.6.6 0 0 1 .6-.6h2.8a.6.6 0 0 1 .6.6v12.8a.6.6 0 0 1-.6.6h-2.8a.6.6 0 0 1-.6-.6Z" />
    </>
  )),
  settings: g24(() => (
    <>
      <g>
        <path d="M12 15a3 3 0 1 0 0-6a3 3 0 0 0 0 6" />
        <path d="m19.622 10.395l-1.097-2.65L20 6l-2-2l-1.735 1.483l-2.707-1.113L12.935 2h-1.954l-.632 2.401l-2.645 1.115L6 4L4 6l1.453 1.789l-1.08 2.657L2 11v2l2.401.656L5.516 16.3L4 18l2 2l1.791-1.46l2.606 1.072L11 22h2l.604-2.387l2.651-1.098C16.697 18.832 18 20 18 20l2-2l-1.484-1.75l1.098-2.652l2.386-.62V11z" />
      </g>
    </>
  )),
  user: g24(() => (
    <>
      <path d="M5 20v-1a7 7 0 0 1 7-7v0a7 7 0 0 1 7 7v1m-7-8a4 4 0 1 0 0-8a4 4 0 0 0 0 8" />
    </>
  )),
  users: g24(() => (
    <>
      <g>
        <path d="M1 20v-1a7 7 0 0 1 7-7v0a7 7 0 0 1 7 7v1" />
        <path d="M13 14v0a5 5 0 0 1 5-5v0a5 5 0 0 1 5 5v.5" />
        <path d="M8 12a4 4 0 1 0 0-8a4 4 0 0 0 0 8m10-3a3 3 0 1 0 0-6a3 3 0 0 0 0 6" />
      </g>
    </>
  )),
  mail: g24(() => (
    <>
      <g>
        <path d="m7 9l5 3.5L17 9" />
        <path d="M2 17V7a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2Z" />
      </g>
    </>
  )),
  calendar: g24(() => (
    <>
      <path d="M15 4V2m0 2v2m0-2h-4.5M3 10v9a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-9zm0 0V6a2 2 0 0 1 2-2h2m0-2v4m14 4V6a2 2 0 0 0-2-2h-.5" />
    </>
  )),
  file: g24(() => (
    <>
      <g>
        <path d="M4 21.4V2.6a.6.6 0 0 1 .6-.6h11.652a.6.6 0 0 1 .424.176l3.148 3.148A.6.6 0 0 1 20 5.75V21.4a.6.6 0 0 1-.6.6H4.6a.6.6 0 0 1-.6-.6M8 10h8m-8 8h8m-8-4h4" />
        <path d="M16 2v3.4a.6.6 0 0 0 .6.6H20" />
      </g>
    </>
  )),
  note: g24(() => (
    <>
      <path d="M8 14h8m-8-4h2m-2 8h4M10 3H6a2 2 0 0 0-2 2v15a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2h-3.5M10 3V1m0 2v2" />
    </>
  )),
  image: g24(() => (
    <>
      <g>
        <path d="M21 3.6v16.8a.6.6 0 0 1-.6.6H3.6a.6.6 0 0 1-.6-.6V3.6a.6.6 0 0 1 .6-.6h16.8a.6.6 0 0 1 .6.6" />
        <path d="m3 16l7-3l11 5m-5-8a2 2 0 1 1 0-4a2 2 0 0 1 0 4" />
      </g>
    </>
  )),
  database: g24(() => (
    <>
      <g>
        <path d="M5 12v6s0 3 7 3s7-3 7-3v-6" />
        <path d="M5 6v6s0 3 7 3s7-3 7-3V6" />
        <path d="M12 3c7 0 7 3 7 3s0 3-7 3s-7-3-7-3s0-3 7-3Z" />
      </g>
    </>
  )),
  cpu: g24(() => (
    <>
      <g>
        <path d="M8 15.4V8.6a.6.6 0 0 1 .6-.6h6.8a.6.6 0 0 1 .6.6v6.8a.6.6 0 0 1-.6.6H8.6a.6.6 0 0 1-.6-.6" />
        <path d="M20 4.6v14.8a.6.6 0 0 1-.6.6H4.6a.6.6 0 0 1-.6-.6V4.6a.6.6 0 0 1 .6-.6h14.8a.6.6 0 0 1 .6.6M17 4V2m-5 2V2M7 4V2m0 18v2m5-2v2m5-2v2m3-5h2m-2-5h2m-2-5h2M4 17H2m2-5H2m2-5H2" />
      </g>
    </>
  )),
  terminal: g24(() => (
    <>
      <path d="M13 17h7M5 7l5 5l-5 5" />
    </>
  )),
  code: g24(() => (
    <>
      <path d="M13.5 6L10 18.5m-3.5-10L3 12l3.5 3.5m11-7L21 12l-3.5 3.5" />
    </>
  )),
  activity: g24(() => (
    <>
      <path d="M3 12h3l3-9l6 18l3-9h3" />
    </>
  )),
  bell: g24(() => (
    <>
      <path d="M18 8.4c0-1.697-.632-3.325-1.757-4.525S13.59 2 12 2s-3.117.674-4.243 1.875C6.632 5.075 6 6.703 6 8.4C6 15.867 3 18 3 18h18s-3-2.133-3-9.6M13.73 21a2 2 0 0 1-3.46 0" />
    </>
  )),
  link: g24(() => (
    <>
      <g>
        <path d="M14 11.998C14 9.506 11.683 7 8.857 7H7.143C4.303 7 2 9.238 2 11.998c0 2.378 1.71 4.368 4 4.873a5.3 5.3 0 0 0 1.143.124" />
        <path d="M10 11.998c0 2.491 2.317 4.997 5.143 4.997h1.714c2.84 0 5.143-2.237 5.143-4.997c0-2.379-1.71-4.37-4-4.874A5.3 5.3 0 0 0 16.857 7" />
      </g>
    </>
  )),
  mic: g24(() => (
    <>
      <g>
        <rect width="6" height="12" x="9" y="2" rx="3" />
        <path d="M5 10v1a7 7 0 0 0 7 7v0a7 7 0 0 0 7-7v-1m-7 8v4m0 0H9m3 0h3" />
      </g>
    </>
  )),
  clock: g24(() => (
    <>
      <g>
        <path d="M12 6v6h6" />
        <path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2S2 6.477 2 12s4.477 10 10 10" />
      </g>
    </>
  )),
  grid: g24(() => (
    <>
      <path d="M14 20.4v-5.8a.6.6 0 0 1 .6-.6h5.8a.6.6 0 0 1 .6.6v5.8a.6.6 0 0 1-.6.6h-5.8a.6.6 0 0 1-.6-.6Zm-11 0v-5.8a.6.6 0 0 1 .6-.6h5.8a.6.6 0 0 1 .6.6v5.8a.6.6 0 0 1-.6.6H3.6a.6.6 0 0 1-.6-.6Zm11-11V3.6a.6.6 0 0 1 .6-.6h5.8a.6.6 0 0 1 .6.6v5.8a.6.6 0 0 1-.6.6h-5.8a.6.6 0 0 1-.6-.6Zm-11 0V3.6a.6.6 0 0 1 .6-.6h5.8a.6.6 0 0 1 .6.6v5.8a.6.6 0 0 1-.6.6H3.6a.6.6 0 0 1-.6-.6Z" />
    </>
  )),
  archive: g24(() => (
    <>
      <g>
        <path d="M7 6h10M7 9h10m-8 8h6" />
        <path d="M3 12h-.4a.6.6 0 0 0-.6.6v8.8a.6.6 0 0 0 .6.6h18.8a.6.6 0 0 0 .6-.6v-8.8a.6.6 0 0 0-.6-.6H21M3 12V2.6a.6.6 0 0 1 .6-.6h16.8a.6.6 0 0 1 .6.6V12M3 12h18" />
      </g>
    </>
  )),
  copy: g24(() => (
    <>
      <g>
        <path d="M19.4 20H9.6a.6.6 0 0 1-.6-.6V9.6a.6.6 0 0 1 .6-.6h9.8a.6.6 0 0 1 .6.6v9.8a.6.6 0 0 1-.6.6" />
        <path d="M15 9V4.6a.6.6 0 0 0-.6-.6H4.6a.6.6 0 0 0-.6.6v9.8a.6.6 0 0 0 .6.6H9" />
      </g>
    </>
  )),
  pin: g24(() => (
    <>
      <path d="M9.5 14.5L3 21M5 9.485l9.193 9.193l1.697-1.697l-.393-3.787l5.51-4.673l-5.85-5.85l-4.674 5.51l-3.786-.393z" />
    </>
  )),
  sun: g24(() => (
    <>
      <path d="M12 18a6 6 0 1 0 0-12a6 6 0 0 0 0 12m10-6h1M12 2V1m0 22v-1m8-2l-1-1m1-15l-1 1M4 20l1-1M4 4l1 1m-4 7h1" />
    </>
  )),
  moon: g24(() => (
    <>
      <path d="M3 11.507a9.493 9.493 0 0 0 18 4.219c-8.507 0-12.726-4.22-12.726-12.726A9.49 9.49 0 0 0 3 11.507" />
    </>
  )),
  system: g24(() => (
    <>
      <g>
        <path d="M2 21h15m4 0h1" />
        <path d="M2 16.4V3.6a.6.6 0 0 1 .6-.6h18.8a.6.6 0 0 1 .6.6v12.8a.6.6 0 0 1-.6.6H2.6a.6.6 0 0 1-.6-.6Z" />
      </g>
    </>
  )),
  library: g24(() => (
    <>
      <g>
        <path d="M4 19V5a2 2 0 0 1 2-2h13.4a.6.6 0 0 1 .6.6v13.114M6 17h14M6 21h14" />
        <path d="M6 21a2 2 0 1 1 0-4" />
        <path d="M9 7h6" />
      </g>
    </>
  )),
};
