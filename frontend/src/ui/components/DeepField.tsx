import { splitProps, type JSX } from "solid-js";
import { cx } from "../cx";

export interface DeepFieldProps {
  class?: string;
}

/** The licensed moment (§11.1) — a graticule, the limb of something large, and a
 *  scatter of stars, drawn behind a whole page.
 *
 *  **It is the page's ground, not a region's.** It was originally cropped to one
 *  region (the launchpad's composer band), which left the limb sliced off at an edge
 *  the composition never anticipated — a horizon is only enormous if you can see it
 *  run the width of the frame. It now backs the entire content column on the screens
 *  that carry it, and the framed surfaces on those screens go **glass** (`.heph-framed`
 *  under `.heph-field-page`) so the field is frosted out from behind anything holding
 *  words. That pairing is the whole reason a page-wide field is not the "atmosphere in
 *  the reading path" failure §11 exists to prevent: nothing readable sits *on* the
 *  field, only in front of a blurred copy of it.
 *
 *  **Still one per screen**, and still only where it backs the page rather than a
 *  panel: the launchpad and the 404 today. Never inside a container holding data, and
 *  deliberately not a prop on `EmptyState` — that is a panel's empty arm, not a
 *  screen's.
 *
 *  **It loses every fight it picks.** The neutral marks run at single-digit alpha and
 *  only the limb takes `--accent`, at 22% — enough that the figure follows the session
 *  mode and any operator accent override, not enough to spend the accent budget (§5.4).
 *
 *  The composition is drawn at **4:3** (1200×900) and anchored **bottom**
 *  (`xMidYMax`). Both choices are about what `slice` throws away. A content column is
 *  roughly square, so `slice` scales the figure by its HEIGHT and eats the width — at
 *  16:10 that was 45% of the composition, which is most of the star field and the ends
 *  of the limb. At 4:3 the crop is a tenth of that. And the limb is the one element
 *  whose position carries meaning, so the vertical crop is spent on the stars above it
 *  rather than on the horizon.
 *
 *  Static by construction. A drifting starfield would be decoration that moves, which
 *  is the one thing worse than decoration, and it would put the human motion register
 *  somewhere the operator is trying to read. Nothing here animates, so there is nothing
 *  for `prefers-reduced-motion` to collapse.
 *
 *  Purely decorative, so `aria-hidden` and `pointer-events-none`: it must never take a
 *  click meant for what sits on top of it.
 */
export function DeepField(props: DeepFieldProps): JSX.Element {
  const [local] = splitProps(props, ["class"]);
  return (
    <svg
      class={cx("heph-deep-field text-bright", local.class)}
      viewBox="0 0 1200 900"
      preserveAspectRatio="xMidYMax slice"
      aria-hidden="true"
    >
      {/* Graticule — the 4px grid's own ancestor, at the scale of a survey chart.
          Square 120px cells: at page scale the figure renders near 1:1, and the
          40px cells the old region-sized field used read as graph paper once they
          covered a whole screen rather than a 320px band. */}
      <g stroke="currentColor" stroke-opacity="0.05" stroke-width="1">
        <path d="M0 120h1200M0 240h1200M0 360h1200M0 480h1200M0 600h1200M0 720h1200M0 840h1200" />
        <path d="M120 0v900M240 0v900M360 0v900M480 0v900M600 0v900M720 0v900M840 0v900M960 0v900M1080 0v900" />
      </g>

      {/* The limb: a body far too large to see the whole of. The ellipse is mostly
          off-canvas on purpose — what makes a horizon read as enormous is that its
          curvature is barely perceptible across the width of the frame. Here that
          is a 66px rise from the edges of the page to its middle.

          The apex sits at 71% rather than at the very bottom: a horizon belongs low,
          but below about 80% it lands behind the launchpad's own panels on every
          window height and is only ever seen frosted. At 71% it crosses the open band
          under the composer, and its flanks still run behind the panels. */}
      <ellipse
        cx="600"
        cy="1540"
        rx="1600"
        ry="900"
        fill="none"
        stroke="var(--accent)"
        stroke-opacity="0.22"
        stroke-width="1"
      />
      <ellipse
        cx="600"
        cy="1590"
        rx="1600"
        ry="900"
        fill="none"
        stroke="currentColor"
        stroke-opacity="0.05"
        stroke-width="1"
      />

      {/* An orbital track ABOVE the limb, not crossing it — its lowest point (470,
          at both ends) clears the limb's highest (640, at the apex) by 170, which
          is deliberate now that the limb sits at 71%: a dashed line intersecting a
          horizon reads as two arcs colliding at this scale, where a track standing
          off it reads as something in orbit over something.

          Dashed, because a predicted path is not a thing you can see — it is a
          thing that has been computed. */}
      <path
        d="M-50 470C280 230 920 230 1250 470"
        fill="none"
        stroke="currentColor"
        stroke-opacity="0.09"
        stroke-width="1"
        stroke-dasharray="3 5"
      />

      {/* Stars thin out toward the limb — the ground glow of something that large
          washes out everything near it, and the gradient is what stops the field
          from reading as an evenly-seeded texture. */}
      <g fill="currentColor">
        <circle cx="93" cy="86" r="1" fill-opacity="0.30" />
        <circle cx="198" cy="148" r="0.8" fill-opacity="0.18" />
        <circle cx="272" cy="62" r="1.2" fill-opacity="0.26" />
        <circle cx="337" cy="210" r="0.8" fill-opacity="0.16" />
        <circle cx="415" cy="110" r="1" fill-opacity="0.24" />
        <circle cx="472" cy="268" r="0.9" fill-opacity="0.14" />
        <circle cx="535" cy="58" r="0.8" fill-opacity="0.28" />
        <circle cx="618" cy="178" r="1.1" fill-opacity="0.20" />
        <circle cx="697" cy="96" r="0.9" fill-opacity="0.26" />
        <circle cx="765" cy="242" r="0.8" fill-opacity="0.15" />
        <circle cx="843" cy="70" r="1" fill-opacity="0.28" />
        <circle cx="913" cy="184" r="0.8" fill-opacity="0.18" />
        <circle cx="998" cy="120" r="1.1" fill-opacity="0.22" />
        <circle cx="1087" cy="64" r="0.8" fill-opacity="0.26" />
        <circle cx="1150" cy="206" r="0.9" fill-opacity="0.16" />
        <circle cx="53" cy="244" r="0.8" fill-opacity="0.20" />
        <circle cx="147" cy="332" r="0.9" fill-opacity="0.15" />
        <circle cx="237" cy="420" r="0.8" fill-opacity="0.13" />
        <circle cx="377" cy="376" r="1" fill-opacity="0.18" />
        <circle cx="503" cy="452" r="0.8" fill-opacity="0.12" />
        <circle cx="640" cy="340" r="0.9" fill-opacity="0.16" />
        <circle cx="737" cy="486" r="0.8" fill-opacity="0.12" />
        <circle cx="870" cy="398" r="1" fill-opacity="0.17" />
        <circle cx="983" cy="468" r="0.8" fill-opacity="0.13" />
        <circle cx="1108" cy="352" r="0.9" fill-opacity="0.15" />
        <circle cx="125" cy="560" r="0.9" fill-opacity="0.12" />
        <circle cx="327" cy="516" r="0.8" fill-opacity="0.11" />
        <circle cx="825" cy="548" r="0.8" fill-opacity="0.11" />
        <circle cx="1057" cy="572" r="0.8" fill-opacity="0.12" />
      </g>
    </svg>
  );
}
