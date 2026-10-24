/** What a conversation grant on a command-running tool is scoped to, for the label and
 *  for the checkbox's own state — **presentation only**.
 *
 *  The backend derives the real scope from the parked call and is the only thing that
 *  writes one down (`services/approval_grants.py`); nothing here is sent to it. This
 *  exists because a checkbox that says "don't ask again for this tool" while the backend
 *  records "don't ask again for `uv run pytest`" is describing a decision the operator is
 *  not making. So the label names the act, and the state is keyed by that act rather than
 *  by the tool — two commands pending in one batch are two separate opt-ins, because they
 *  will be two separate grants.
 *
 *  It reads **less** than the backend's grammar walk and must never read it *differently*:
 *  a command carrying anything the shell would expand, quote or chain is unscopeable here
 *  and falls back to the plain label, which is the honest thing to say when we cannot name
 *  the act. What it does read, it reads by the backend's own three rules — an environment
 *  assignment in front of the program is not a word of the act, a flag is one of them, and
 *  the cap counts operands — because a label derived by a *different* rule is not a vaguer
 *  description of the recorded grant, it is a description of something else. `CI=1 bun
 *  test` was the case that proved it: read here as its own act, recorded there as `bun
 *  test`, and keyed as neither.
 *
 *  It cannot promise the grant will exist. The backend refuses a scope for reasons no
 *  reading of the words can see — a path leaving the worktree, a command run on the host —
 *  and says so on the approve response, which is where that is reported (`stream/
 *  approvals.ts`). This file names the act; it does not vouch for it.
 */

/** How many *operands* the backend keeps: the program and the words naming the mode it
 *  was invoked in. Flags are kept beside them and count against nothing. */
const PREFIX_OPERANDS = 3;

/** Anything the shell reads as syntax, expansion or quoting. A command carrying one of
 *  these is not something this file can name — the words as written are not the words the
 *  program receives, and the tail of a pipeline is a second act the label would hide. */
const UNREADABLE = /[\\{}[\]*?`'"$;|&<>()\n]/;

/** An environment assignment written in front of the program (`CI=1 bun test`). The shell
 *  hands it to the environment rather than to the command, and the backend's walk reads it
 *  as exactly that — so a label that opened with it would name an act the recorded grant
 *  does not. */
const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;

/** An option rather than an operand. A bare `-` is stdin and `--` ends the options;
 *  neither is a flag, and reading them as one would invent a flag no program has. */
const isFlag = (word: string) =>
  word.startsWith("-") && word !== "-" && word !== "--";

/** The leading words of `command`, or null when it cannot be named.
 *
 *  The same three rules the backend's walk applies to the subset of commands this file
 *  will read at all: environment assignments in front of the program are not words of the
 *  act, a flag is kept but costs the cap nothing, and the cap counts operands — after
 *  three of them nothing more is read, because that is where a prefix stops naming the act
 *  and starts naming its target. */
export function commandPrefix(command: string): string[] | null {
  const trimmed = command.trim();
  if (!trimmed || UNREADABLE.test(trimmed)) return null;
  const words: string[] = [];
  let operands = 0;
  for (const word of trimmed.split(/\s+/)) {
    if (words.length === 0 && ASSIGNMENT.test(word)) continue;
    if (operands >= PREFIX_OPERANDS) break;
    words.push(word);
    // The first word is the program, whatever it looks like; only after it does a
    // leading dash mean an option — and a bare `-` or `--` never does.
    if (words.length === 1 || !isFlag(word)) operands += 1;
  }
  return words.length > 0 ? words : null;
}

/** How far an opt-in reaches, as the card asks for it.
 *
 *  `"off"` is no standing yes at all. `"command"` is the default tick — the act this call
 *  is, which on a tool that runs a command is that command and on any other tool is simply
 *  the tool. `"tool"` is the wider pick, offered only where the two differ: everything the
 *  tool runs, for the rest of the thread.
 *
 *  It is the *width* and not the key: which act an opt-in belongs to is `grantKey`'s
 *  answer, and holding the width in the value keeps one checkbox per act however the
 *  operator sets it. */
export type GrantWidth = "off" | "command" | "tool";

/** The width a plain tick asks for, before the operator touches the picker.
 *
 *  Two answers, and the second is the load-bearing one. Where the call runs a command the
 *  narrow width is the default and the picker offers the wider one beside it. Where it
 *  runs **no** command there is nothing narrower to be scoped to — the tool *is* the act,
 *  and the checkbox says so — so the tick means the wider width outright. Recording the
 *  narrower one there would leave the label promising a standing yes that the level doing
 *  the deciding does not honour, which is the shape of bug the widths were added to fix. */
export const tickedWidth = (command: string | undefined): GrantWidth =>
  command ? "command" : "tool";

/** The act a grant would name, as one string — "uv run pytest" — or null. */
export function commandScopeLabel(command: string | undefined): string | null {
  if (!command) return null;
  const words = commandPrefix(command);
  return words ? words.join(" ") : null;
}

/** The key one opt-in is held under.
 *
 *  Three cases, because two of them used to be one. A call that names an act shares its
 *  checkbox with every other call naming the same act — that is the point, and it matches
 *  the single grant the backend records for them. A call to a tool that runs **no** command
 *  keys on the tool, which is exactly the whole-tool grant the backend writes for it. And a
 *  call whose command this file cannot read keys on the **call**: the backend still derives
 *  a scope of its own from it (its walk reads far more than the regex above), so two such
 *  commands pending together are two separate grants, and one checkbox driving both would
 *  record a standing yes to a command the operator never ticked. */
export function grantKey(
  toolName: string,
  command: string | undefined,
  toolCallId: string,
): string {
  if (!command) return toolName;
  const scope = commandScopeLabel(command);
  return scope ? `${toolName} ${scope}` : `call:${toolCallId}`;
}
