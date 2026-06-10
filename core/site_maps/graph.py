"""Navigation-graph + modality helpers (Phase 3 enrichment), pure.

# OWNS: turning a flat affordance list into a small navigation graph (sections
#        by heading + entry-point links), and the accessibility-vs-screenshot
#        modality recommendation.
# NOT OWNS: rendering, the actual vision-model resolve, persistence.
# DECISIONS: a page with very few a11y nodes is likely canvas/image-driven, so a
#   screenshot+vision pass would help — we recommend it; executing that vision
#   pass needs multimodal-LLM plumbing and is a separate (deferred) concern.
"""

from __future__ import annotations

from typing import Any

SITE_MAP_GRAPH_SCHEMA = "aztea/site-map/2"
# Below this many accessibility nodes the page is probably canvas/image-only, so
# text reasoning over the a11y tree is unreliable and vision would do better.
_MIN_AX_NODES_FOR_TEXT = 8
_ENTRY_POINT_CAP = 20


def build_navigation_graph(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Pure: group flattened a11y rows into heading-led sections + entry links.

    A light graph a planner can reason over ("which section, which link leads
    where") without re-reading the whole tree. Links/buttons before the first
    heading land in a leading section with heading=None.
    """
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def _ensure() -> dict[str, Any]:
        nonlocal current
        if current is None:
            current = {"heading": None, "links": [], "buttons": []}
            sections.append(current)
        return current

    for row in rows:
        role = row.get("role", "")
        name = row.get("name", "")
        if role == "heading":
            current = {"heading": name, "links": [], "buttons": []}
            sections.append(current)
        elif role == "link" and name:
            _ensure()["links"].append(name)
        elif role == "button" and name:
            _ensure()["buttons"].append(name)
    entry_points = [
        r.get("name", "") for r in rows if r.get("role") == "link" and r.get("name")
    ][:_ENTRY_POINT_CAP]
    return {"schema": SITE_MAP_GRAPH_SCHEMA, "sections": sections, "entry_points": entry_points}


def recommend_modality(rows: list[dict[str, str]]) -> str:
    """Pure: 'accessibility_tree' when the a11y tree is rich enough to reason over,
    else 'screenshot' to signal that a vision pass would do better (canvas/image
    pages). This is advice — the navigator still extracts from whatever it has.
    """
    return "accessibility_tree" if len(rows) >= _MIN_AX_NODES_FOR_TEXT else "screenshot"
