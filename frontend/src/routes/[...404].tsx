import { Button, ConstructionReveal, Text } from "~/ui";

/** A path with no route. The surfaces that became settings sections used to
 *  forward through here into `?settings=…`; nothing is deployed anywhere that
 *  could be holding a link to one, so a dead path reads as dead. */
export default function NotFound() {
  return (
    // No fill: the ambient field (§11.1) is this screen's ground.
    <div class="relative flex h-screen items-center justify-center overflow-hidden text-text">
      {/* The plate is the View's own panel — `ConstructionReveal`: registration
          marks, hairline rules, and the frosted `.heph-glass` surface inside them.
          It is reused rather than reproduced on `RegistrationFrame`, which draws
          marks and no surface: a second "framed glass panel" in the system is two
          things to keep in step, and the frost keeps the field out of the words.

          `when` is a constant true. Everywhere else this component gates a region
          the operator deliberately opens, and the entry gesture says *a place was
          made, and then filled*; a 404 is not opened at all, but it is the one
          screen where the product has nothing to do but assemble itself in front
          of someone who arrived by mistake. There is no exit, because there is no
          state in which this screen closes. */}
      <ConstructionReveal
        when
        origin="top-left"
        // NOT `isolate` and no `z-index`: either makes this a backdrop root and
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
