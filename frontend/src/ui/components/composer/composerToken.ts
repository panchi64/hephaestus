/** Reading a `/` or `@` trigger out of what is in the field.
 *
 *  Pure, and split out from the Composer for the reason every rule in this codebase ends
 *  up in its own file: the wrong answer still renders a menu. A trigger that fires one
 *  character too eagerly turns a file path into a command picker, and one that fires too
 *  late never opens at all — both look like "the menu is a bit flaky" rather than like a
 *  bug with a location.
 *
 *  **Two triggers, two rules, and they are deliberately not the same.**
 *
 *  `/` opens only at the very start of the field or at the start of a line. A slash is a
 *  path separator and a division sign; anywhere else in a sentence it is overwhelmingly
 *  likelier to be `src/foo.ts` than a command, and a picker that opened over the middle of
 *  a typed path would be wrong far more often than right.
 *
 *  `@` opens after any whitespace, or at the start. It has no such second life in prose —
 *  an operator typing `@` mid-sentence in a code thread means a file — but it *does* have
 *  one inside a word (`user@host`, `pkg@1.2.0`), which is why the character before it has
 *  to be whitespace rather than merely "not a letter".
 *
 *  **The query stops at whitespace, and that is what closes the menu.** Neither a command
 *  name nor a path contains a space, so the first space ends the token: `/reviewer check
 *  the auth path` is a command with an argument, not a four-word command. The caller reads
 *  that as "the operator finished choosing" — the picker closes and the rest of the line
 *  is theirs to type.
 */

/** Which menu a token asks for. */
export type ComposerTrigger = "/" | "@";

export interface ComposerToken {
  trigger: ComposerTrigger;
  /** What has been typed after the trigger — never contains whitespace. */
  query: string;
  /** Index of the trigger character itself. */
  start: number;
  /** Index one past the end of the query — always the caret. */
  end: number;
}

/** True when `char` may sit directly before a trigger for it to count.
 *
 *  Whitespace only, never "not a word character": `user@host` and `v1/v2` both have a
 *  non-word character in front and neither is a trigger. */
const opensAfter = (char: string): boolean => /\s/.test(char);

/** The token the caret is currently inside, or null.
 *
 *  Reads **backwards from the caret**, not forwards from the trigger, because the caret is
 *  the only thing that says which token the operator is editing. A field holding two
 *  slashes is editing whichever one they are standing in.
 */
export function tokenAt(text: string, caret: number): ComposerToken | null {
  const at = Math.max(0, Math.min(caret, text.length));
  for (let i = at - 1; i >= 0; i -= 1) {
    const char = text[i]!;
    // Whitespace before finding a trigger means the caret is in an ordinary word —
    // which is also how a menu closes once the operator types the space after a name.
    if (/\s/.test(char)) return null;
    if (char !== "/" && char !== "@") continue;
    const before = i === 0 ? "" : text[i - 1]!;
    // A trigger character in a position that cannot open a menu is **skipped, not
    // terminal** — the scan keeps looking further back. Only whitespace ends it.
    //
    // This is the whole of `@src/ui/comp`: a path is exactly what an `@` query is, so its
    // separators sit between the caret and the `@` that opened it. Reading the first `/`
    // as "no token here" made every multi-segment path close the picker on the character
    // that needed it most.
    //
    // A `/` opens only at the start of a line — anywhere else it is a path separator or a
    // division sign, and a picker over a typed path is wrong on every keystroke of one.
    if (char === "/" && i !== 0 && before !== "\n") continue;
    // An `@` opens after whitespace only, never inside a word: `bob@example.com` and
    // `vite@7.3` are not file references.
    if (char === "@" && i !== 0 && !opensAfter(before)) continue;
    return { trigger: char, query: text.slice(i + 1, at), start: i, end: at };
  }
  return null;
}

/** One highlighted run of the field's text. */
export interface TokenSpan {
  trigger: ComposerTrigger;
  start: number;
  /** One past the last character of the token. */
  end: number;
}

/** **Every** command and file token in the text, in order — not just the one the caret
 *  is in.
 *
 *  `tokenAt` answers "what is the operator choosing right now" and reads backwards from
 *  the caret; this answers "what has been named in this message" and is what the accent
 *  highlight is drawn from. A message can carry several — `/review @src/a.ts @src/b.ts` —
 *  and all of them are named things, whether or not the caret happens to be in one.
 *
 *  The rules are the same two `tokenAt` applies, which is the point of them living beside
 *  each other: a `/` only at the start of a line, an `@` only after whitespace, and a
 *  token ending at the first space. A token with nothing after its trigger is not
 *  highlighted — a bare `@` the operator has only just typed is not yet a reference, and
 *  colouring it makes the accent flicker on every word boundary.
 */
export function tokenSpans(text: string): TokenSpan[] {
  const spans: TokenSpan[] = [];
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i]!;
    if (char !== "/" && char !== "@") continue;
    const before = i === 0 ? "" : text[i - 1]!;
    if (char === "/" && i !== 0 && before !== "\n") continue;
    if (char === "@" && i !== 0 && !opensAfter(before)) continue;
    let end = i + 1;
    while (end < text.length && !/\s/.test(text[end]!)) end += 1;
    if (end === i + 1) continue; // a bare trigger names nothing yet
    spans.push({ trigger: char, start: i, end });
    i = end;
  }
  return spans;
}

export interface TokenReplacement {
  text: string;
  /** Where the caret goes afterwards — always just past what was inserted. */
  caret: number;
}

/** Swap a token for what the operator picked, and say where the caret lands.
 *
 *  `insert` is the text without the trigger; the trigger is kept, so a replaced token
 *  still reads as the thing it is (`/reviewer`, `@src/app.tsx`). One trailing space is
 *  added when the next character is not already whitespace — picking a command is almost
 *  always followed by typing its argument, and it also *closes the menu*, since the space
 *  is what ends the token.
 */
export function replaceToken(
  text: string,
  token: ComposerToken,
  insert: string,
): TokenReplacement {
  const rest = text.slice(token.end);
  const spacer = rest.startsWith(" ") || rest.startsWith("\n") ? "" : " ";
  const head = `${text.slice(0, token.start)}${token.trigger}${insert}${spacer}`;
  return { text: `${head}${rest}`, caret: head.length };
}
