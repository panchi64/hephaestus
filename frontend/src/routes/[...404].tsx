import { Button, Plate, Text } from "~/ui";

/** A path with no route. The surfaces that became settings sections used to
 *  forward through here into `?settings=…`; nothing is deployed anywhere that
 *  could be holding a link to one, so a dead path reads as dead. */
export default function NotFound() {
  return (
    // No fill: the ambient field (§11.1) is this screen's ground.
    <div class="relative flex h-screen items-center justify-center overflow-hidden text-text">
      <Plate
        assetId="HEPH-ERR-404"
        class="w-full max-w-md"
        contentClass="flex flex-col items-center gap-3 p-8 text-center"
      >
        {/* `text-bright`, not alert (§10.4, §5 rule 1): a wrong address is not a
            fault — nothing is running, failing, or waiting on the operator. */}
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
      </Plate>
    </div>
  );
}
