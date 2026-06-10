/**
 * TS half of the TD1 parity guard. Runs the shared fixture
 * (../../deference/classification-fixtures.json) through the ported
 * classifier and asserts it matches the spec the Python classifier is also
 * tested against (tests/test_deference_parity_fixture.py). Keep both green.
 *
 * Test runner: vitest (matches the OpenClaw repo). When this plugin is
 * published as its own package, wire this into that package's CI so the port
 * can't drift.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { classifyPretoolEventForMode, normalizeToolName } from "./classifier.ts";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
  readFileSync(join(here, "..", "..", "deference", "classification-fixtures.json"), "utf-8"),
) as {
  cases: Array<{
    event: { tool_name: string; tool_input?: Record<string, unknown> };
    mode: "block" | "warn" | "block-all";
    expect: { decision: "block" | "warn" | "allow"; category?: string };
  }>;
};

describe("deference classifier parity (TS side of TD1)", () => {
  for (const c of fixture.cases) {
    const cmd = (c.event.tool_input?.command as string) ?? "";
    it(`${c.event.tool_name} ${cmd} / ${c.mode} -> ${c.expect.decision}`, () => {
      const decision = classifyPretoolEventForMode(c.event.tool_name, c.event.tool_input, c.mode);
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

describe("OpenClaw native tool-name normalization", () => {
  // Without this map the plugin never fires: OpenClaw emits web_fetch /
  // web_search / exec (param `command`), not the canonical names.
  it("maps the wedge tools to canonical names", () => {
    expect(normalizeToolName("web_fetch")).toBe("WebFetch");
    expect(normalizeToolName("web_search")).toBe("WebSearch");
    expect(normalizeToolName("exec")).toBe("Bash");
    expect(normalizeToolName("browser")).toBe("WebFetch");
  });

  it("passes canonical and unknown names through", () => {
    expect(normalizeToolName("WebFetch")).toBe("WebFetch");
    expect(normalizeToolName("read")).toBe("read");
  });

  it("classifies an OpenClaw exec curl as a live_data wedge", () => {
    const d = classifyPretoolEventForMode(
      normalizeToolName("exec"),
      { command: "curl https://example.com" },
      "block-all",
    );
    expect(d).not.toBeNull();
    expect(d!.action).toBe("block");
    expect(d!.category).toBe("live_data");
  });
});
