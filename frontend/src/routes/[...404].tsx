import { Button, ConstructionReveal, DeepField, Text } from "~/ui";

/** A path with no route. The surfaces that became settings sections used to
 *  forward through here into `?settings=…`; nothing is deployed anywhere that
 *  could be holding a link to one, so a dead path reads as dead.
 *
 *  **This screen carries §11.1's licensed moment**, and it is the clearest case the
 *  rule has: a 404 is a whole viewport with one small plate on it and nothing running,
 *  so there is nothing for the field to compete with or sit in front of. It is also
 *  the one screen the operator reaches by accident, which is the right place for the
 *  product to be quietly good-looking rather than merely correct.
 *
 *  It draws its own field rather than going through `isDeepFieldRoute`: this route is
 *  outside the `(app)` group, so there is no shell column here to paint behind. */
export default function NotFound() {
  return (
    <div class="relative flex h-screen items-center justify-center overflow-hidden bg-bg text-text">
      <DeepField />
      {/* The plate is the View's own panel — `ConstructionReveal`: registration
          marks, hairline rules, and the frosted `.heph-glass` surface inside them.
          It is reused rather than reproduced on `RegistrationFrame`, which draws
          marks and no surface: a second "framed glass panel" in the system is two
          things to keep in step, and the frost is the whole point here. The field
          runs right under this plate, so a surface that merely tinted would leave
          a graticule crossing the words.

          `when` is a constant true. Everywhere else this component gates a region
          the operator deliberately opens, and the entry gesture says *a place was
          made, and then filled*; a 404 is not opened at all, but it is the one
          screen where the product has nothing to do but assemble itself in front
          of someone who arrived by mistake. There is no exit, because there is no
          state in which this screen closes. */}
      <ConstructionReveal
        when
        origin="top-left"
        // `relative`: the field is an absolutely positioned sibling and would
        // otherwise paint over in-flow content in the same stacking context.
        // NOT `isolate` and no `z-index` — either makes this a backdrop root and
        // the glass would frost nothing but itself (see `.heph-glass`).
        class="relative w-full max-w-md"
        contentClass="flex flex-col items-center gap-3 p-8 text-center"
      >
        {/* The hero readout is `text-bright`, not alert (§10.4, §5 rule 1): a
            screen at rest is grayscale, and a wrong address is not a fault —
            nothing is running, failing, or waiting on the operator here. */}
        <Text variant="readout-lg" tone="bright">
          404
        </Text>
        <Text variant="label" tone="dim">
          No such route
        </Text>
        <Text variant="body" tone="dim">
          The requested surface does not exist or has been decommissioned.
        </Text>
        <Button variant="default" href="/" trailing="u-turn" class="mt-2">
          Return to overview
        </Button>
        {/* The diegetic id, kept from the `RegistrationFrame` this replaced. In
            flow at the foot of the plate rather than absolutely centred on its
            bottom edge: this frame's rules sit 6px inside the region, and a line
            pinned to the region's edge would have crossed one of them.

            `plate`, not `micro` plus hand-rolled `uppercase tracking-label`: that
            is the variant this voice already has (§4 — it NAMES a region), so the
            id tracks `--tracking-plate` with every other engraved legend in the
            product instead of drifting off on its own. */}
        <Text variant="plate" tone="dim" class="mt-2">
          HEPH-ERR-404
        </Text>
      </ConstructionReveal>
    </div>
  );
}
