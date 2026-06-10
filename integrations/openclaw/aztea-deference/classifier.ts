/**
 * Pure deference classifier — TS port of the Python source of truth in
 * sdks/python-sdk/aztea/cli/deference_core.py (classify_pretool_event +
 * classify_pretool_event_for_mode).
 *
 * TD1 (parity, not subprocess): this runs IN-PROCESS on every tool call (a
 * per-call subprocess would add 150-600ms and get the plugin uninstalled).
 * The shared fixture integrations/deference/classification-fixtures.json is
 * run through BOTH this port (index.test.ts) and the Python classifier
 * (tests/test_deference_parity_fixture.py); as long as both stay green the
 * port cannot drift and under-detect wedge tasks. Edit the fixture, not the
 * logic, and keep both sides green.
 *
 * This module is dependency-free on purpose: the parity test (and plain
 * `node`) can import it without resolving the OpenClaw plugin SDK.
 */

export type Decision = { action: "block" | "warn"; category: string; message: string };
export type DeferenceMode = "warn" | "block" | "block-all";

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

/**
 * OpenClaw's native tool names → the canonical (Claude-shaped) names the
 * classifier understands. Without this map the plugin never fires: OpenClaw
 * emits `web_fetch` / `web_search` / `exec` (param `command`), not
 * WebFetch / WebSearch / Bash. `browser` counts as web — it is a live-page
 * fetch by other means.
 */
const OPENCLAW_TOOL_NAME_MAP: Record<string, string> = {
  web_fetch: "WebFetch",
  web_search: "WebSearch",
  exec: "Bash",
  browser: "WebFetch",
};

export function normalizeToolName(toolName: string): string {
  return OPENCLAW_TOOL_NAME_MAP[toolName] ?? toolName;
}

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

/**
 * Direct port of deference_core.classify_pretool_event_for_mode — the single
 * place the warn/block/block-all vocabulary is interpreted. "block-all"
 * escalates every wedge category to block (experiment treatment arm); unknown
 * modes degrade to warn semantics (fail-open, never throw).
 */
export function classifyPretoolEventForMode(
  toolName: string,
  toolInput: Record<string, unknown> | undefined,
  mode: string,
): Decision | null {
  const allowBlock = mode === "block" || mode === "block-all";
  const decision = classifyPretoolEvent(toolName, toolInput, allowBlock);
  if (decision === null || mode !== "block-all") return decision;
  if (decision.action === "block") return decision;
  return { action: "block", category: decision.category, message: decision.message };
}
