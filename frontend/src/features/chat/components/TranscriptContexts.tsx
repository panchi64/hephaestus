import type { Accessor, JSX } from "solid-js";
import type { ToolImage } from "../model";
import { ScreenshotContext } from "../screenshots";
import { TurnLockContext } from "../turnLock";

/** What every turn in the transcript reads from its conversation: whether a turn is
 *  live, and the screenshots an answer may embed. */
export function TranscriptContexts(props: {
  sending: Accessor<boolean>;
  screenshots: Accessor<Map<string, ToolImage>>;
  children: JSX.Element;
}): JSX.Element {
  return (
    <TurnLockContext.Provider value={props.sending}>
      <ScreenshotContext.Provider value={props.screenshots}>
        {props.children}
      </ScreenshotContext.Provider>
    </TurnLockContext.Provider>
  );
}
