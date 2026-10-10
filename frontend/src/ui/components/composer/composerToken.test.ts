import { describe, expect, test } from "bun:test";
import { replaceToken, tokenAt, tokenSpans } from "./composerToken";

/** Where `|` stands is the caret; the rest is what is in the field. Fixtures read as the
 *  thing the operator is looking at, which is the only way a case like `src/foo|` is
 *  obviously about a path rather than about a command. */
function at(fixture: string) {
  const caret = fixture.indexOf("|");
  if (caret < 0) throw new Error("fixture needs a | for the caret");
  return tokenAt(fixture.slice(0, caret) + fixture.slice(caret + 1), caret);
}

describe("the slash trigger", () => {
  test("opens at the start of an empty field", () => {
    expect(at("/|")).toMatchObject({ trigger: "/", query: "" });
  });

  test("carries what has been typed after it", () => {
    expect(at("/revi|")).toMatchObject({
      trigger: "/",
      query: "revi",
      start: 0,
    });
  });

  test("opens at the start of a later line", () => {
    expect(at("some notes\n/comp|")).toMatchObject({
      trigger: "/",
      query: "comp",
    });
  });

  // The case the rule exists for. A slash mid-sentence is a path or a division sign far
  // more often than it is a command, and a picker opening over a typed path is wrong on
  // every keystroke of it.
  test("does NOT open inside a path", () => {
    expect(at("look at src/foo|")).toBeNull();
  });

  test("does NOT open after a space", () => {
    expect(at("check /revi|")).toBeNull();
  });
});

describe("the at trigger", () => {
  test("opens at the start of the field", () => {
    expect(at("@src/ap|")).toMatchObject({ trigger: "@", query: "src/ap" });
  });

  test("opens mid-sentence after a space", () => {
    expect(at("please read @back|")).toMatchObject({
      trigger: "@",
      query: "back",
    });
  });

  test("opens after a newline", () => {
    expect(at("notes\n@src|")).toMatchObject({ trigger: "@", query: "src" });
  });

  // `@` has no second life in prose, but it does inside a word — so the character before
  // it has to be whitespace, not merely "not a letter".
  test("does NOT open inside an email address", () => {
    expect(at("mail me at bob@exa|")).toBeNull();
  });

  test("does NOT open inside a pinned version", () => {
    expect(at("install vite@7.3|")).toBeNull();
  });

  // A path is exactly what an @-query is, so the slash inside one must not be read as a
  // second trigger sitting closer to the caret.
  test("a slash inside an at-query does not steal the token", () => {
    expect(at("@src/ui/comp|")).toMatchObject({
      trigger: "@",
      query: "src/ui/comp",
    });
  });
});

describe("closing", () => {
  test("a space after the name ends the token", () => {
    // This is what dismisses the picker: the operator has chosen, and the rest of the
    // line is the command's argument rather than more of its name.
    expect(at("/reviewer check|")).toBeNull();
  });

  test("plain prose is never a token", () => {
    expect(at("just a message|")).toBeNull();
  });

  test("an empty field is never a token", () => {
    expect(at("|")).toBeNull();
  });
});

describe("the caret decides which token is being edited", () => {
  test("a caret back inside the first of two tokens reads that one", () => {
    // Reading forwards from the first trigger would answer this the same way by luck;
    // the second case below is the one that separates the two readings.
    expect(at("@one| @two")).toMatchObject({ query: "one" });
  });

  test("a caret in the second of two tokens reads the second", () => {
    expect(at("@one @two|")).toMatchObject({ query: "two" });
  });
});

describe("tokenSpans", () => {
  const spans = (text: string) =>
    tokenSpans(text).map((s) => text.slice(s.start, s.end));

  test("finds a command and every file reference", () => {
    // All of them, not just the one the caret is in — the accent marks what has been
    // named in the message, which is a different question from what is being chosen.
    expect(spans("/review @src/a.ts and @src/b.ts")).toEqual([
      "/review",
      "@src/a.ts",
      "@src/b.ts",
    ]);
  });

  test("applies the same two rules `tokenAt` does", () => {
    expect(spans("look at src/foo.ts")).toEqual([]);
    expect(spans("mail bob@example.com")).toEqual([]);
    expect(spans("vite@7.3.6")).toEqual([]);
  });

  test("a bare trigger names nothing yet", () => {
    // Otherwise the accent flickers on at every word boundary as the operator types.
    expect(spans("@")).toEqual([]);
    expect(spans("/")).toEqual([]);
    expect(spans("read @ now")).toEqual([]);
  });

  test("a slash opens on a later line too", () => {
    expect(spans("notes\n/compact")).toEqual(["/compact"]);
  });

  test("a token ends at whitespace, so the argument is not coloured", () => {
    expect(spans("/reviewer check the auth path")).toEqual(["/reviewer"]);
  });

  test("spans are in order and never overlap", () => {
    const text = "/a @b @c";
    const found = tokenSpans(text);
    expect(found.map((s) => s.start)).toEqual([0, 3, 6]);
    for (let i = 1; i < found.length; i += 1) {
      expect(found[i]!.start).toBeGreaterThanOrEqual(found[i - 1]!.end);
    }
  });
});

describe("replaceToken", () => {
  test("keeps the trigger and puts the caret past the insertion", () => {
    const token = tokenAt("/revi", 5)!;
    const next = replaceToken("/revi", token, "reviewer");
    expect(next.text).toBe("/reviewer ");
    expect(next.caret).toBe(next.text.length);
  });

  test("adds one trailing space, so the menu closes on the pick", () => {
    const token = tokenAt("@src/ap", 7)!;
    expect(replaceToken("@src/ap", token, "src/app.tsx").text).toBe(
      "@src/app.tsx ",
    );
  });

  test("does not double a space that is already there", () => {
    const text = "@src later";
    const token = tokenAt(text, 4)!;
    const next = replaceToken(text, token, "src/app.tsx");
    expect(next.text).toBe("@src/app.tsx later");
    // The caret sits at the end of what was inserted, not at the end of the line — the
    // operator was editing a token in the middle of a sentence they had already typed.
    expect(next.caret).toBe("@src/app.tsx".length);
  });

  test("preserves what came before the token", () => {
    const text = "please read @back";
    const token = tokenAt(text, text.length)!;
    expect(replaceToken(text, token, "backend/app.py").text).toBe(
      "please read @backend/app.py ",
    );
  });
});
