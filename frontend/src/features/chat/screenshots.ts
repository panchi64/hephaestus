import { createContext, useContext, type Accessor } from "solid-js";
import type { ChatMessage, ToolImage, ToolInvocation } from "./model";

/** Every screenshot in the conversation, keyed by the tool call that took it. An answer
 *  embeds one as `![…](screenshot:<tool call id>)`, which may name a call from an
 *  earlier turn or one a script made. */
export function screenshotIndex(
  messages: readonly ChatMessage[],
): Map<string, ToolImage> {
  const index = new Map<string, ToolImage>();
  const visit = (tool: ToolInvocation): void => {
    const first = tool.images?.[0];
    if (first) index.set(tool.id, first);
    tool.children?.forEach(visit);
  };
  for (const message of messages)
    for (const block of message.blocks ?? [])
      if (block.kind === "tool") visit(block.tool);
  return index;
}

export const ScreenshotContext = createContext<
  Accessor<Map<string, ToolImage>>
>(() => new Map());

/** The `src` for an embedded screenshot, or undefined when this view doesn't hold it. */
export function useScreenshotSrc(): (id: string) => string | undefined {
  const index = useContext(ScreenshotContext);
  return (id) => {
    const image = index().get(id);
    return image && `data:${image.mediaType};base64,${image.data}`;
  };
}
