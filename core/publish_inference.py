"""
publish_inference.py — pure inference engine for `aztea publish` and the
`/publish_agent` MCP tool.

# OWNS: AST-based inference of publishable agent metadata (name, slug,
#       description, input_schema, output_schema, price, category, tags)
#       from a Python handler source string + optional natural-language hint.
# NOT OWNS: file I/O (callers read the source), HTTP (callers POST to the
#       backend), LLM calls (this engine is deterministic on purpose so
#       golden-file tests pin its behavior).
# INVARIANTS:
#   - Pure function: same input ⇒ same output. No randomness, no time, no env
#     reads inside the inference path. Golden-file tests depend on this.
#   - Never raises on malformed source — return an InferredAgentSpec with the
#     `missing` tuple populated instead. The publish flow surfaces missing
#     fields back to the caller (the MCP tool's multi-turn contract relies
#     on this).
#   - JSON Schema output is conservative — unknown / un-annotated types map
#     to `{"type": "string"}` with a confidence note, NOT to `Any` (which
#     would fail server-side validation later).
# DECISIONS:
#   - We use `ast` (stdlib) rather than `typing.get_type_hints` because the
#     latter requires importing the module, which would execute arbitrary
#     publisher code at inference time. AST-only means we can infer over
#     untrusted source safely.
#   - Default price = $0.05 to match the existing CLI default at
#     sdks/python-sdk/aztea/cli/publish.py:541.
#   - Default category = "developer-tools" — the broadest curated bucket.
#     Keyword heuristics narrow to "security" / "data" / "web" / "auth"
#     when the source signals it.

Both consumers — the MCP `/publish_agent` tool and the `aztea publish`
wizard mode — call the same `infer()` entrypoint, so the inference
contract is one shape everywhere. Tests live in
`tests/test_publish_inference.py` with golden files under
`tests/fixtures/publish_inference/`.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field
from typing import Any

# Public surface — keep this small. Callers should not import private helpers
# (they are not stable).
__all__ = ["InferredAgentSpec", "infer", "DEFAULT_PRICE_USD", "DEFAULT_CATEGORY"]


# ─── Module-level constants ────────────────────────────────────────────────
#
# Match the existing CLI default so that switching the CLI to wizard mode
# does not surprise long-time publishers who relied on the previous default.
DEFAULT_PRICE_USD: float = 0.05
DEFAULT_CATEGORY: str = "developer-tools"

# When the inference can't pick a category from source signals.
_MAX_TAGS = 5
_MAX_DESCRIPTION_LEN = 240

# Heuristic category keywords. First match wins, so ordering matters: place
# the narrower / higher-signal categories before the broad ones.
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("security", ("cve", "vulnerab", "exploit", "secret", "audit", "scanner", "owasp", "xss", "sql_injection", "ssrf")),
    ("auth", ("jwt", "oauth", "saml", "openid", "session", "token", "auth")),
    ("data", ("parser", "extract", "schema", "csv", "json", "pdf", "spreadsheet", "etl")),
    ("web", ("http", "scrape", "browser", "lighthouse", "accessibility", "dom", "selenium", "playwright")),
)

# Common English stopwords + Python/code stopwords. Tags should be content
# words, not filler. Kept short on purpose — the heuristic is a hint, not a
# search engine.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "to", "of", "in", "on", "at",
    "for", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "this", "that", "these", "those", "it", "its", "into",
    "out", "via", "per", "any", "all", "no", "not", "do", "does", "doing",
    "given", "given:", "input", "output", "return", "returns", "args", "kwarg",
    "kwargs", "self", "cls", "true", "false", "none", "dict", "list", "str",
    "int", "float", "bool",
})


# ─── Public dataclass ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class InferredAgentSpec:
    """The output of `infer()`. All fields populated for every call.

    Even when inference cannot determine a value (e.g. no docstring, no
    annotations), the field is populated with a safe default and the
    `confidence` map records the source ("filename" / "module_docstring" /
    "fallback" / "unknown"). The `missing` tuple lists fields whose
    confidence is "fallback" or "unknown" — those are the ones the caller
    (the MCP tool or the CLI wizard) should surface to the user for
    manual override.
    """
    name: str
    slug: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    price_per_call_usd: float
    category: str
    tags: tuple[str, ...]
    confidence: dict[str, str] = field(default_factory=dict)
    missing: tuple[str, ...] = field(default_factory=tuple)

    def to_jsonable(self) -> dict[str, Any]:
        """Return a JSON-serializable dict — used by the golden-file tests.

        Tuples are converted to lists so the JSON round-trip is stable. The
        order of keys matches the field declaration order so the golden
        files diff cleanly.
        """
        d = asdict(self)
        d["tags"] = list(self.tags)
        d["missing"] = list(self.missing)
        return d


# ─── Entry point ───────────────────────────────────────────────────────────


def infer(
    handler_source: str,
    *,
    hint: str | None = None,
    filename: str | None = None,
) -> InferredAgentSpec:
    """Infer agent metadata from a Python handler source string.

    Inputs:
      handler_source: raw Python source text — usually the contents of a
        `handler.py` file the publisher wrote. Must be syntactically valid
        Python (we use `ast.parse`); if parsing fails, every field is
        populated from defaults and `missing` lists them all.
      hint: optional natural-language hint from the caller ("this scans
        Dockerfiles for security issues"). Used to nudge category +
        description if the source itself is sparse. NOT injected into
        prompt context — there is no LLM here.
      filename: optional original filename. Used to derive name + slug if
        the function name is generic ("handler"). When omitted, we fall
        back to the function name.

    Returns: an InferredAgentSpec — never raises.

    The function is intentionally deterministic: same inputs always
    produce the same output (down to dict key ordering inside schemas,
    via sorted-keys at JSON-encoding time). Golden-file tests in
    `tests/test_publish_inference.py` lock this in.
    """
    source = handler_source if isinstance(handler_source, str) else ""
    hint_text = (hint or "").strip()

    # Step 1: parse. Failure ⇒ fully-fallback spec.
    tree = _safe_parse(source)
    if tree is None:
        return _fully_fallback_spec(hint_text, filename)

    # Step 2: pick the handler function. Prefer `handler`; otherwise the
    # only public function; otherwise mark the spec as ambiguous.
    handler_fn, fn_picker_confidence = _pick_handler_function(tree)

    # Step 3: docstrings.
    module_doc = _safe_get_docstring(tree)
    fn_doc = _safe_get_docstring(handler_fn) if handler_fn else None

    # Step 4: name + slug.
    name, name_confidence = _infer_name(filename, handler_fn)
    slug = _kebab_slug(name)

    # Step 5: description.
    description, desc_confidence = _infer_description(fn_doc, module_doc, hint_text)

    # Step 6: schemas.
    pydantic_models = _collect_pydantic_models(tree)
    input_schema, input_confidence = _infer_input_schema(handler_fn, pydantic_models)
    output_schema, output_confidence = _infer_output_schema(handler_fn, pydantic_models)

    # Step 7: category + tags.
    category, cat_confidence = _infer_category(description, source, hint_text)
    tags = _infer_tags(description, name, hint_text)

    confidence = {
        "name": name_confidence,
        "slug": "derived_from_name",
        "description": desc_confidence,
        "input_schema": input_confidence,
        "output_schema": output_confidence,
        "price_per_call_usd": "default",
        "category": cat_confidence,
        "tags": "derived_from_description",
    }
    if fn_picker_confidence == "ambiguous":
        confidence["handler_function"] = "ambiguous_multiple_public_functions"
    elif fn_picker_confidence == "none":
        confidence["handler_function"] = "no_function_found"

    missing = _missing_fields(confidence, name, description, input_schema, output_schema)

    return InferredAgentSpec(
        name=name,
        slug=slug,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        price_per_call_usd=DEFAULT_PRICE_USD,
        category=category,
        tags=tuple(tags),
        confidence=confidence,
        missing=tuple(missing),
    )


# ─── AST helpers ───────────────────────────────────────────────────────────


def _safe_parse(source: str) -> ast.Module | None:
    """ast.parse with a try/except — keeps `infer()` non-raising."""
    if not source.strip():
        return None
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _safe_get_docstring(node: Any) -> str | None:
    if node is None:
        return None
    try:
        doc = ast.get_docstring(node)
    except (TypeError, AttributeError):
        return None
    return doc.strip() if doc else None


def _pick_handler_function(
    tree: ast.Module,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef | None, str]:
    """Return (function_node, confidence_label).

    Confidence labels:
      "handler_named"   — function is literally `def handler(...)`
      "only_public"     — single public function in the module
      "ambiguous"       — multiple public functions; spec.missing reports
      "none"            — module has no top-level function
    """
    public_fns: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == "handler":
            return node, "handler_named"
        if not node.name.startswith("_"):
            public_fns.append(node)
    if len(public_fns) == 1:
        return public_fns[0], "only_public"
    if len(public_fns) > 1:
        # Prefer one named "run" or "main" as a secondary fallback.
        for preferred_name in ("run", "main", "execute"):
            for fn in public_fns:
                if fn.name == preferred_name:
                    return fn, "only_public"
        return public_fns[0], "ambiguous"
    return None, "none"


def _collect_pydantic_models(tree: ast.Module) -> dict[str, dict[str, Any]]:
    """Map class_name → JSON schema for any pydantic BaseModel subclasses.

    Detection is by base-class name string match (we do not import the
    module, so we cannot check the MRO). False positives are unlikely —
    publishers do not name their unrelated base classes `BaseModel`.
    """
    out: dict[str, dict[str, Any]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(
            (isinstance(b, ast.Name) and b.id == "BaseModel")
            or (isinstance(b, ast.Attribute) and b.attr == "BaseModel")
            for b in node.bases
        ):
            continue
        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []
        for item in node.body:
            if not isinstance(item, ast.AnnAssign):
                continue
            if not isinstance(item.target, ast.Name):
                continue
            field_name = item.target.id
            properties[field_name] = _annotation_to_json_schema(item.annotation)
            # No default value ⇒ field is required.
            if item.value is None:
                required.append(field_name)
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = sorted(required)
        out[node.name] = schema
    return out


# ─── Type → JSON Schema ────────────────────────────────────────────────────


def _annotation_to_json_schema(annotation: ast.expr | None) -> dict[str, Any]:
    """Translate an `ast`-style type annotation to a JSON schema fragment.

    Supports the common cases: primitives (str, int, float, bool), generics
    (list[X], dict[str, X]), Optional/Union/Literal. Unknown annotations
    fall back to {"type": "string"} — chosen because the listing-safety
    probe later builds adversarial payloads and an over-permissive `object`
    schema masks real validation problems.
    """
    if annotation is None:
        return {"type": "string"}
    return _annotation_node_to_schema(annotation)


def _annotation_node_to_schema(node: ast.expr) -> dict[str, Any]:  # noqa: C901
    if isinstance(node, ast.Constant) and node.value is None:
        return {"type": "null"}
    if isinstance(node, ast.Name):
        return _name_to_schema(node.id)
    if isinstance(node, ast.Attribute):
        # e.g. `typing.List[int]` — flatten to the right-most attr name.
        return _name_to_schema(node.attr)
    if isinstance(node, ast.Subscript):
        return _subscript_to_schema(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        # PEP 604: `int | None`, `str | bytes`, ...
        left = _annotation_node_to_schema(node.left)
        right = _annotation_node_to_schema(node.right)
        return _union_schemas(left, right)
    if isinstance(node, ast.Tuple):
        # Bare tuple in annotations is rare and ambiguous; treat as Any.
        return {"type": "string"}
    return {"type": "string"}


def _name_to_schema(name: str) -> dict[str, Any]:
    lowered = name.lower()
    if lowered in {"str", "string"}:
        return {"type": "string"}
    if lowered in {"int", "integer"}:
        return {"type": "integer"}
    if lowered in {"float", "number"}:
        return {"type": "number"}
    if lowered in {"bool", "boolean"}:
        return {"type": "boolean"}
    if lowered in {"none", "nonetype"}:
        return {"type": "null"}
    if lowered in {"dict", "mapping"}:
        return {"type": "object", "additionalProperties": True}
    if lowered in {"list", "sequence", "tuple"}:
        return {"type": "array", "items": {"type": "string"}}
    if lowered in {"any", "object"}:
        return {"type": "string"}
    # Likely a user-defined class — return a Pydantic-style $ref-shaped hint
    # the caller may resolve against the module's pydantic model registry.
    return {"type": "object", "x-aztea-inferred-class": name}


def _subscript_to_schema(node: ast.Subscript) -> dict[str, Any]:
    container = _generic_container_name(node.value)
    slice_node = node.slice
    if container in {"List", "list", "Sequence", "Iterable", "Tuple"}:
        item_schema = _annotation_node_to_schema(_unwrap_index(slice_node))
        return {"type": "array", "items": item_schema}
    if container in {"Dict", "dict", "Mapping"}:
        # Dict[K, V] — only V matters for JSON schema (keys are strings).
        slice_expr = _unwrap_index(slice_node)
        if isinstance(slice_expr, ast.Tuple) and len(slice_expr.elts) == 2:
            value_schema = _annotation_node_to_schema(slice_expr.elts[1])
            return {"type": "object", "additionalProperties": value_schema}
        return {"type": "object", "additionalProperties": True}
    if container in {"Optional",}:
        inner = _annotation_node_to_schema(_unwrap_index(slice_node))
        return _union_schemas(inner, {"type": "null"})
    if container in {"Union",}:
        slice_expr = _unwrap_index(slice_node)
        if isinstance(slice_expr, ast.Tuple):
            schemas = [_annotation_node_to_schema(e) for e in slice_expr.elts]
            result = schemas[0]
            for s in schemas[1:]:
                result = _union_schemas(result, s)
            return result
        return _annotation_node_to_schema(slice_expr)
    if container in {"Literal",}:
        slice_expr = _unwrap_index(slice_node)
        values: list[Any] = []
        candidates = slice_expr.elts if isinstance(slice_expr, ast.Tuple) else [slice_expr]
        for c in candidates:
            if isinstance(c, ast.Constant):
                values.append(c.value)
        if values:
            return {"type": _literal_value_type(values), "enum": values}
    # Unknown generic — fall back to the bare name.
    return _name_to_schema(container)


def _generic_container_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _unwrap_index(node: ast.expr) -> ast.expr:
    """Python 3.9+ subscript slices are plain expressions; pre-3.9 wrap them
    in `ast.Index`. Strip the wrapper if present so callers see the inner."""
    if hasattr(ast, "Index") and isinstance(node, ast.Index):  # type: ignore[attr-defined]
        return node.value  # type: ignore[attr-defined]
    return node


def _union_schemas(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Combine two schemas with anyOf, deduping the trivial case."""
    if a == b:
        return a
    a_types = a.get("type")
    b_types = b.get("type")
    # Optional-style merge: T | null ⇒ {"type": [..., "null"]}
    if a == {"type": "null"} and isinstance(b_types, str):
        return {"type": [b_types, "null"]}
    if b == {"type": "null"} and isinstance(a_types, str):
        return {"type": [a_types, "null"]}
    return {"anyOf": [a, b]}


def _literal_value_type(values: list[Any]) -> str:
    if all(isinstance(v, bool) for v in values):
        return "boolean"
    if all(isinstance(v, int) for v in values):
        return "integer"
    if all(isinstance(v, float) for v in values):
        return "number"
    if all(isinstance(v, str) for v in values):
        return "string"
    return "string"


# ─── Schema inference for the chosen function ──────────────────────────────


def _infer_input_schema(
    fn: ast.FunctionDef | ast.AsyncFunctionDef | None,
    pydantic_models: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    if fn is None:
        return {"type": "object", "properties": {}}, "fallback"
    # If the function has a single annotated parameter that resolves to a
    # collected pydantic model, use that model's schema directly.
    if len(fn.args.args) == 1:
        single = fn.args.args[0]
        if single.annotation is not None:
            annotation_node = single.annotation
            if isinstance(annotation_node, ast.Name) and annotation_node.id in pydantic_models:
                return pydantic_models[annotation_node.id], "pydantic_model"
            # Single dict / Mapping param → pass-through object schema.
            if isinstance(annotation_node, (ast.Name, ast.Subscript, ast.Attribute)):
                schema = _annotation_node_to_schema(annotation_node)
                if schema.get("type") == "object":
                    return schema, "single_dict_param"
    # Otherwise, walk every parameter and build a schema with each as a key.
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    defaults_offset = len(fn.args.args) - len(fn.args.defaults)
    for idx, arg in enumerate(fn.args.args):
        # Skip `self` / `cls` for class-based handlers.
        if idx == 0 and arg.arg in {"self", "cls"}:
            continue
        properties[arg.arg] = _annotation_to_json_schema(arg.annotation)
        if idx < defaults_offset:
            required.append(arg.arg)
    if not properties:
        return {"type": "object", "properties": {}}, "fallback"
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = sorted(required)
    return schema, "function_signature"


def _infer_output_schema(
    fn: ast.FunctionDef | ast.AsyncFunctionDef | None,
    pydantic_models: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    if fn is None or fn.returns is None:
        return {"type": "object"}, "fallback"
    annotation = fn.returns
    if isinstance(annotation, ast.Name) and annotation.id in pydantic_models:
        return pydantic_models[annotation.id], "pydantic_model"
    schema = _annotation_node_to_schema(annotation)
    return schema, "return_annotation"


# ─── Naming, description, category, tags ───────────────────────────────────


def _infer_name(
    filename: str | None,
    fn: ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> tuple[str, str]:
    if filename:
        stem = re.sub(r"\.(py|skill\.md|md)$", "", filename.split("/")[-1])
        stem = stem.strip()
        if stem:
            words = re.split(r"[_\-\s]+", stem)
            name = " ".join(w.capitalize() for w in words if w)
            return name, "filename"
    if fn and fn.name and fn.name != "handler":
        words = re.split(r"[_\-]+", fn.name)
        name = " ".join(w.capitalize() for w in words if w)
        return name, "function_name"
    return "Untitled Agent", "fallback"


def _infer_description(
    fn_doc: str | None,
    module_doc: str | None,
    hint_text: str,
) -> tuple[str, str]:
    for source_text, source_label in (
        (fn_doc, "function_docstring"),
        (module_doc, "module_docstring"),
        (hint_text, "user_hint"),
    ):
        if source_text:
            first_line = source_text.strip().split("\n", 1)[0].strip()
            if first_line:
                if len(first_line) > _MAX_DESCRIPTION_LEN:
                    first_line = first_line[: _MAX_DESCRIPTION_LEN - 1].rstrip() + "…"
                return first_line, source_label
    return "", "fallback"


def _infer_category(description: str, source: str, hint_text: str) -> tuple[str, str]:
    haystack = " ".join((description, source, hint_text)).lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in haystack:
                return category, "keyword_match"
    return DEFAULT_CATEGORY, "fallback"


def _infer_tags(description: str, name: str, hint_text: str) -> list[str]:
    text = " ".join((description, name, hint_text)).lower()
    tokens = re.findall(r"[a-z][a-z0-9]{2,}", text)
    counts: dict[str, int] = {}
    for tok in tokens:
        if tok in _STOPWORDS:
            continue
        counts[tok] = counts.get(tok, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [tok for tok, _ in ranked[:_MAX_TAGS]]


def _kebab_slug(name: str) -> str:
    if not name:
        return "untitled-agent"
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "untitled-agent"


def _missing_fields(
    confidence: dict[str, str],
    name: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    if confidence.get("name") == "fallback" or name == "Untitled Agent":
        out.append("name")
    if confidence.get("description") == "fallback" or not description:
        out.append("description")
    if confidence.get("input_schema") == "fallback":
        out.append("input_schema")
    if confidence.get("output_schema") == "fallback":
        out.append("output_schema")
    # Category falling back to the default is OK — publishers rarely care.
    # Schemas with no properties signal genuine inability to infer.
    if input_schema.get("properties") == {} and "input_schema" not in out:
        out.append("input_schema")
    return out


# ─── Fully-fallback construction ───────────────────────────────────────────


def _fully_fallback_spec(hint_text: str, filename: str | None) -> InferredAgentSpec:
    """Return a spec with every field at its default when source parse fails.

    The hint is still used for description/category so an "all defaults"
    publish is at least named after the user's intent if they typed one.
    """
    name, name_confidence = _infer_name(filename, None)
    slug = _kebab_slug(name)
    description, desc_confidence = _infer_description(None, None, hint_text)
    category, cat_confidence = _infer_category(description, "", hint_text)
    tags = _infer_tags(description, name, hint_text)
    confidence = {
        "name": name_confidence,
        "slug": "derived_from_name",
        "description": desc_confidence,
        "input_schema": "fallback",
        "output_schema": "fallback",
        "price_per_call_usd": "default",
        "category": cat_confidence,
        "tags": "derived_from_description",
        "handler_function": "source_unparseable",
    }
    missing = _missing_fields(
        confidence, name, description,
        {"type": "object", "properties": {}},
        {"type": "object"},
    )
    return InferredAgentSpec(
        name=name,
        slug=slug,
        description=description,
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
        price_per_call_usd=DEFAULT_PRICE_USD,
        category=category,
        tags=tuple(tags),
        confidence=confidence,
        missing=tuple(missing),
    )
