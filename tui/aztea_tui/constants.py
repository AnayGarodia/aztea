STATUS_STYLES: dict[str, str] = {
    "pending":                "#3b82f6 bold",
    "running":                "#f59e0b bold",
    "awaiting_clarification": "#06b6d4 bold",
    "complete":               "#10b981 bold",
    "failed":                 "#ef4444 bold",
    "unknown":                "#64748b",
}

HEALTH_ICON: dict[str, str] = {
    "ok":      "●",
    "degraded":"◑",
    "error":   "○",
    "unknown": "—",
}

NAV_ITEMS: list[tuple[str, str, str]] = [
    ("1", "agents",    "  Agents"),
    ("2", "jobs",      "  Jobs"),
    ("3", "wallet",    "  Wallet"),
    ("4", "my-agents", "  My Agents"),
]

TERMINAL_STATUSES = frozenset({"complete", "failed"})
