import { describe, expect, test } from "bun:test";
import { uploadForm } from "./data";

describe("the upload form", () => {
  const file = new File(["owed 3,120"], "taxes.txt", { type: "text/plain" });

  test("files a private composer's upload as private", () => {
    const form = uploadForm(file, true);
    expect(form.get("private")).toBe("true");
    expect((form.get("file") as File).name).toBe("taxes.txt");
  });

  test("leaves an ordinary composer's upload unmarked", () => {
    expect(uploadForm(file, false).has("private")).toBe(false);
  });
});
