import assert from "node:assert/strict";
import test from "node:test";

import { normalizeName } from "../src/normalize.js";

test("normalizes null input", () => {
  assert.equal(normalizeName(null), "");
});
