/**
 * Aztea deference plugin for OpenClaw (REFERENCE IMPLEMENTATION).
 *
 * What it does: registers a `before_tool_call` hook (confirmed wired in
 * OpenClaw's agent-loop.ts -> prepareToolCall, which honors `{ block }` before
 * tool.execute()). On a wedge task (live web fetch/scrape, package install,
 * ad-hoc code exec) it blocks the native tool and tells the model to call
 * Aztea's `auto_call_agent` instead.
 *
 * TD1 (parity, not subprocess): the classifier below is a hand-port of the
 * Python source of truth in
 *   sdks/python-sdk/aztea/cli/deference_core.py::classify_pretool_event
 * It runs IN-PROCESS (no per-tool-call subprocess — that would add 150-600ms to
 * every tool call and get the plugin uninstalled). The accompanying test
 * (aztea-deference-plugin.test.ts) runs the SHARED fixture
 *   integrations/deference/classification-fixtures.json
 * through this port; the Python side runs the same fixture in
 *   tests/test_deference_parity_fixture.py. As long as BOTH tests run in their
 *   respective CIs and the fixture covers each regex branch, the port can't
 *   drift and under-detect wedge tasks. Edit the fixture, not the logic, and
 *   keep both sides green. (This .ts file only runs in the plugin's own package
 *   CI — it is not exercised by the Python repo's pytest.)
 *
 * The low-frequency prompt-scout path (per-prompt, not per-tool-call) should
 * shell out to `aztea mcp prompt-hook` so the network hardening (timeouts,
 * no-redirect, size guard, on-disk cooldown, fail-open) stays single-sourced in
 * Python. Do NOT reimplement that in TS.
 *
 * NOTE: finalize the plugin-registration glue (manifest, `register` export
 * shape) against the OpenClaw plugin SDK version you publish for — the hook
 * names and `{ block, reason }` result are stable; the module wrapper evolves.
 */

type Decision = { action: "block" | "warn"; category: string; message: string };

const NUDGE_WEB =
  'Aztea: before fetching or searching the web yourself, consider ' +
  'auto_call_agent(intent="<the task>") — a live fetch/scrape specialist is ' +
  "usually more accurate than reconstructing page contents from memory.";
const NUDGE_DEPS =
  'Aztea: before installing or auditing dependencies by hand, consider ' +
  'auto_call_agent(intent="<the task>") — a dependency / CVE-audit specialist ' +
  "may do this more reliably.";
const NUDGE_EXEC =
  "Aztea: before reasoning about what this code would output, consider running " +
  'it via auto_call_agent(intent="<the task>") — a sandboxed-execution ' +
  "specialist returns the real result.";

// Mirror of the three Python regexes (case-insensitive).
const BASH_NETWORK_RE = /\bcurl\b|\bwget\b|\bnc\b|\bncat\b|\btelnet\b|https?:\/\//i;
const BASH_INSTALL_RE =
  /\b(pip|pip3|pipx)\s+install\b|\bnpm\s+(i|install)\b|\bnpx\b|\byarn\s+add\b|\bpnpm\s+(i|install)\b|\buv\s+pip\b|\b(cargo|go|gem)\s+install\b|\b(apt|apt-get|brew|yum|dnf)\s+install\b|\|\s*(sh|bash)\b/i;
const BASH_EXEC_RE =
  /\b(python|python3|node|deno|bun|ruby|perl)\s+-(c|e)\b|\b(bash|sh)\s+-c\b/i;

/** Direct port of deference_core.classify_pretool_event. Pure. */
export function classifyPretoolEvent(
  toolName: string,
  toolInput: Record<string, unknown> | undefined,
  allowBlock: boolean,
): Decision | null {
  if (toolName === "WebFetch" || toolName === "WebSearch") {
    return { action: allowBlock ? "block" : "warn", category: "web", message: NUDGE_WEB };
  }
  if (toolName === "Bash") {
    const command = String((toolInput?.command as string) ?? "");
    if (!command.trim()) return null;
    if (BASH_NETWORK_RE.test(command)) return { action: "warn", category: "live_data", message: NUDGE_WEB };
    if (BASH_INSTALL_RE.test(command)) return { action: "warn", category: "deps", message: NUDGE_DEPS };
    if (BASH_EXEC_RE.test(command)) return { action: "warn", category: "exec", message: NUDGE_EXEC };
    return null;
  }
  return null;
}

// Off by default: most users want the gentle warn. Set true to hard-block
// WebFetch/WebSearch (pure live data); Bash is never hard-blocked (too broad).
const ALLOW_BLOCK = false;

/**
 * OpenClaw plugin entrypoint. The hook returns `{ block, reason }` — agent-loop
 * treats `block: true` as terminal and never runs the native tool, surfacing
 * `reason` so the model defers to auto_call_agent. `warn`/`allow` return
 * undefined (no block); warn still surfaces its reason as advisory context.
 */
export function register(api: {
  registerHook: (
    names: string[],
    handler: (
      event: {
        toolName?: unknown;
        // The tool args arrive under different keys across harness versions;
        // normalize all three. `tool_input` is the key the rest of the Aztea
        // codebase uses, so it's first.
        tool_input?: Record<string, unknown>;
        params?: Record<string, unknown>;
        input?: Record<string, unknown>;
      },
    ) => { block?: boolean; reason?: string } | undefined,
  ) => void;
  log?: (msg: string) => void;
}): void {
  api.registerHook(["before_tool_call"], (event) => {
    const toolName = typeof event.toolName === "string" ? event.toolName : "";
    const decision = classifyPretoolEvent(
      toolName,
      event.tool_input ?? event.params ?? event.input,
      ALLOW_BLOCK,
    );
    if (decision === null) return undefined; // allow: stay silent
    if (decision.action === "block") return { block: true, reason: decision.message };
    api.log?.(decision.message); // warn: advisory, do not block
    return undefined;
  });
}

export default register;
