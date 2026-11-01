export interface TextSpan {
  text: string;
  code: boolean;
}

/** `text` split on single-backtick code spans. An unmatched backtick stays literal. */
export function codeSpans(text: string): TextSpan[] {
  const spans: TextSpan[] = [];
  const pattern = /`([^`]+)`/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) {
      spans.push({ text: text.slice(last, match.index), code: false });
    }
    spans.push({ text: match[1], code: true });
    last = match.index + match[0].length;
  }
  if (last < text.length) spans.push({ text: text.slice(last), code: false });
  return spans;
}
