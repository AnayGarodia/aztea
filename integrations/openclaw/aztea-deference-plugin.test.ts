/**
 * TS half of the TD1 parity guard. Runs the shared fixture
 * (../deference/classification-fixtures.json) through the ported
 * classifyPretoolEvent and asserts it matches the spec the Python classifier is
 * also tested against (tests/test_deference_parity_fixture.py). Keep both green.
 *
 * Test runner: vitest (matches the OpenClaw repo). When you publish this plugin
 * as its own package, wire this into that package's CI so the port can't drift.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { classifyPretoolEvent } from "./aztea-deference-plugin";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
  readFileSync(join(here, "..", "deference", "classification-fixtures.json"), "utf-8"),
) as {
  cases: Array<{
    event: { tool_name: string; tool_input?: Record<string, unknown> };
    mode: "block" | "warn";
    expect: { decision: "block" | "warn" | "allow"; category?: string };
  }>;
};

describe("deference classifier parity (TS side of TD1)", () => {
  for (const c of fixture.cases) {
    const cmd = (c.event.tool_input?.command as string) ?? "";
    it(`${c.event.tool_name} ${cmd} / ${c.mode} -> ${c.expect.decision}`, () => {
      const decision = classifyPretoolEvent(
        c.event.tool_name,
        c.event.tool_input,
        c.mode === "block",
      );
      if (c.expect.decision === "allow") {
        expect(decision).toBeNull();
      } else {
        expect(decision).not.toBeNull();
        expect(decision!.action).toBe(c.expect.decision);
        expect(decision!.category).toBe(c.expect.category);
      }
    });
  }
});
