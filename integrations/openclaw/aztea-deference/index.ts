/**
 * Aztea deference plugin for OpenClaw.
 *
 * Registers a `before_tool_call` hook (wired in OpenClaw's agent-loop ->
 * prepareToolCall, which honors `{ block, blockReason }` before
 * tool.execute()). On a wedge task (live web fetch/scrape, package install,
 * ad-hoc code exec) it blocks the native tool and tells the model to call
 * Aztea's `auto_call_agent` instead.
 *
 * Mode (config `mode` in plugins.entries["aztea-deference"].config, falling
 * back to the AZTEA_DEFERENCE_MODE env var):
 *   warn      — observe + log only, never blocks (DEFAULT). The deference
 *               log then shows where the agent hits wedge tasks — the pull
 *               signal — without taxing tools the model already uses well.
 *   block     — hard-block WebFetch/WebSearch-class tools (opt-in).
 *   block-all — hard-block every wedge category (experiment instrument
 *               only — the 2026-06-10 experiment showed blocking commodity
 *               tools is net-negative; see experiments/deference/REPORT.md).
 *
 * Observability: each wedge decision is also fed to
 * `aztea mcp pretool-hook --format json` via a fire-and-forget spawn so the
 * decision row lands in ~/.aztea/deference.jsonl with the schema single-
 * sourced in Python. The spawn is fail-open and never awaited — the
 * in-process classifier above is authoritative for the block decision (the
 * shared parity fixture guarantees the two agree).
 *
 * The low-frequency prompt-scout path (per-prompt, not per-tool-call) should
 * shell out to `aztea mcp prompt-hook` so the network hardening (timeouts,
 * no-redirect, size guard, on-disk cooldown, fail-open) stays single-sourced
 * in Python. Do NOT reimplement that in TS.
 */
import { spawn } from "node:child_process";

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { classifyPretoolEventForMode, normalizeToolName } from "./classifier.ts";

// Pull, not push: default is observe-only. The 2026-06-10 deference
// experiment (experiments/deference/REPORT.md) showed hard-blocking
// commodity tools is net-negative (slower, ~2x tokens, equal-at-best
// correctness); the catalog the model can elect is the product surface.
const DEFAULT_MODE = "warn";

function resolveMode(registerConfigMode: unknown, event: unknown, ctx: unknown): string {
  // api.pluginConfig (resolved at register time) is the authoritative source;
  // the hooks docs also describe per-handler event.context.pluginConfig, so
  // tolerate that and ctx too, then the env var, then the default.
  const fromEvent = (event as { context?: { pluginConfig?: { mode?: unknown } } })?.context
    ?.pluginConfig?.mode;
  const fromCtx = (ctx as { pluginConfig?: { mode?: unknown } })?.pluginConfig?.mode;
  const fromEnv = process.env.AZTEA_DEFERENCE_MODE;
  const mode = registerConfigMode ?? fromEvent ?? fromCtx ?? fromEnv ?? DEFAULT_MODE;
  return typeof mode === "string" && mode ? mode : DEFAULT_MODE;
}

/**
 * Fire-and-forget: pipe the normalized event through the Python CLI purely so
 * the decision row lands in ~/.aztea/deference.jsonl (single log schema).
 * spawn(argv) — never a shell string; tool inputs are attacker-influenced.
 * Fail-open: any error here must never affect the hook decision.
 */
function recordDecisionAsync(
  toolName: string,
  toolInput: Record<string, unknown> | undefined,
  mode: string,
): void {
  try {
    const child = spawn("aztea", ["mcp", "pretool-hook", "--mode", mode, "--format", "json"], {
      stdio: ["pipe", "ignore", "ignore"],
      detached: false,
      // Tag the deference-log row with the harness identity; the MCP server
      // entry's env does not reach this plugin-spawned subprocess.
      env: { ...process.env, AZTEA_CLIENT_ID: process.env.AZTEA_CLIENT_ID ?? "openclaw" },
    });
    child.on("error", () => {});
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify({ tool_name: toolName, tool_input: toolInput ?? {} }));
    child.unref();
  } catch {
    // Logging must never break the hook (fail-open invariant).
  }
}

export default definePluginEntry({
  id: "aztea-deference",
  name: "Aztea Deference",
  description:
    "Defers wedge tasks (live web fetch/scrape, package installs, ad-hoc code exec) to Aztea specialists via auto_call_agent.",
  register(api: {
    on: (
      name: string,
      handler: (
        event: { toolName?: unknown; params?: Record<string, unknown> },
        ctx?: unknown,
      ) =>
        | { block?: boolean; blockReason?: string; reason?: string }
        | undefined,
      opts?: { priority?: number },
    ) => void;
    pluginConfig?: { mode?: unknown };
    logger?: { info?: (msg: string) => void };
  }) {
    const registerConfigMode = api.pluginConfig?.mode;
    api.on("before_tool_call", (event, ctx) => {
      const rawName = typeof event.toolName === "string" ? event.toolName : "";
      const toolName = normalizeToolName(rawName);
      const toolInput = event.params;
      const mode = resolveMode(registerConfigMode, event, ctx);
      const decision = classifyPretoolEventForMode(toolName, toolInput, mode);
      if (decision === null) return undefined; // allow: stay silent
      recordDecisionAsync(toolName, toolInput, mode);
      if (decision.action === "block") {
        // agent-loop reads `blockReason` (falls back to a generic string
        // without it); `reason` is kept for older harness versions.
        return { block: true, blockReason: decision.message, reason: decision.message };
      }
      api.logger?.info?.(decision.message); // warn: advisory, do not block
      return undefined;
    });
  },
});
