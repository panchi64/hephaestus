import { firstLine } from "./toolSummary";

const BLOCK_MARKER = /^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)/;
const RULE_OR_FENCE = /^\s{0,3}(?:`{3,}|~{3,}|[-*_](?:\s*[-*_]){2,}\s*$)/;
const LINK = /!?\[([^\]]*)\]\([^)]*\)/g;
// No single `_`: it would eat the underscores of a snake_case word.
const EMPHASIS = /(\*\*|__|\*|~~|`)(\S(?:.*?\S)?)\1/g;

function plainLine(line: string): string {
  if (RULE_OR_FENCE.test(line)) return "";
  return line
    .replace(BLOCK_MARKER, "")
    .replace(LINK, "$1")
    .replace(EMPHASIS, "$2")
    .trim();
}

/** The first line of a markdown text with its syntax stripped, as a one-row preview. */
export function markdownPreview(markdown: string): string | undefined {
  const line = markdown
    .split("\n")
    .map(plainLine)
    .find((l) => l.length > 0);
  return line ? firstLine(line) : undefined;
}
