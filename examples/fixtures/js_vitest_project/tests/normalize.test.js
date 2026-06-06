import { expect, test } from "vitest";
import { normalizeName } from "../src/normalize.js";

test("normalizes null input", () => {
  expect(normalizeName(null)).toBe("");
});
