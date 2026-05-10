"""Discovery-driven contract tests for every pydantic v2 model in core/models.

# OWNS: schema validity + roundtrip invariants for every BaseModel subclass
#       reachable from core.models. Drives ~110 models × multiple assertions.
# INVARIANTS asserted:
#   - model.model_json_schema() returns a dict with stable shape (idempotent).
#   - The schema declares "properties" or "$defs" (valid pydantic v2 output).
#   - For models we can construct from defaults, model_validate(model_dump())
#     roundtrips equal.
#   - model_fields is non-empty for every model (otherwise the model is
#     trivially useless and signals a copy-paste mistake).
"""
from __future__ import annotations

import importlib
import json
import pkgutil

import pytest
from pydantic import BaseModel, ValidationError

import core.models

pytestmark = pytest.mark.property


def _discover_models() -> list[type[BaseModel]]:
    seen: dict[str, type[BaseModel]] = {}
    for _, name, _ in pkgutil.walk_packages(core.models.__path__, prefix="core.models."):
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        for attr in dir(mod):
            cls = getattr(mod, attr)
            if (
                isinstance(cls, type)
                and issubclass(cls, BaseModel)
                and cls is not BaseModel
                and cls.__module__ == name
            ):
                seen[f"{name}.{attr}"] = cls
    return sorted(seen.values(), key=lambda c: f"{c.__module__}.{c.__name__}")


_MODELS = _discover_models()
_MODEL_IDS = [f"{m.__module__.split('.')[-1]}.{m.__name__}" for m in _MODELS]


# Some models are intentionally pass-through containers with no declared
# fields (e.g., responses that wrap arbitrary objects). Allowlist them so
# the no-fields test stays meaningful for the rest of the catalogue.
_FIELDLESS_ALLOWLIST = {
    "core.models.responses.DynamicObjectResponse",
}


@pytest.mark.parametrize("model_cls", _MODELS, ids=_MODEL_IDS)
def test_model_has_fields(model_cls):
    """Every discovered model declares at least one field — otherwise it's a
    placeholder and probably a mistake. Documented exceptions in
    `_FIELDLESS_ALLOWLIST`."""
    fqname = f"{model_cls.__module__}.{model_cls.__name__}"
    if fqname in _FIELDLESS_ALLOWLIST:
        return
    assert model_cls.model_fields, f"{model_cls.__name__} declares no fields"


@pytest.mark.parametrize("model_cls", _MODELS, ids=_MODEL_IDS)
def test_model_schema_returns_dict(model_cls):
    schema = model_cls.model_json_schema()
    assert isinstance(schema, dict)


@pytest.mark.parametrize("model_cls", _MODELS, ids=_MODEL_IDS)
def test_model_schema_idempotent(model_cls):
    """Generating the schema twice returns the same dict — no shared mutable state."""
    a = model_cls.model_json_schema()
    b = model_cls.model_json_schema()
    assert a == b


@pytest.mark.parametrize("model_cls", _MODELS, ids=_MODEL_IDS)
def test_model_schema_is_json_serializable(model_cls):
    schema = model_cls.model_json_schema()
    s = json.dumps(schema)
    assert json.loads(s) == schema


@pytest.mark.parametrize("model_cls", _MODELS, ids=_MODEL_IDS)
def test_model_schema_has_properties_or_refs(model_cls):
    """Pydantic v2 emits 'properties' (object schema) or '$defs' / '$ref' (composed)."""
    schema = model_cls.model_json_schema()
    has_properties = "properties" in schema
    has_refs = "$defs" in schema or "$ref" in schema or "anyOf" in schema or "oneOf" in schema
    assert has_properties or has_refs, (
        f"{model_cls.__name__} schema has no properties or refs: {schema}"
    )


def _can_construct_with_defaults(model_cls: type[BaseModel]) -> bool:
    """A model qualifies if (a) every field is optional/has a default, AND
    (b) calling it with no args actually succeeds (no model_validator or
    cross-field constraint blocks it)."""
    for field in model_cls.model_fields.values():
        if field.is_required():
            return False
    try:
        model_cls()
    except Exception:
        return False
    return True


_DEFAULT_CONSTRUCTABLE = [m for m in _MODELS if _can_construct_with_defaults(m)]
_DEFAULT_IDS = [
    f"{m.__module__.split('.')[-1]}.{m.__name__}" for m in _DEFAULT_CONSTRUCTABLE
]


@pytest.mark.parametrize("model_cls", _DEFAULT_CONSTRUCTABLE, ids=_DEFAULT_IDS)
def test_default_constructable_roundtrip(model_cls):
    """Models with all-optional fields must roundtrip through dump/validate."""
    instance = model_cls()
    dumped = instance.model_dump()
    restored = model_cls.model_validate(dumped)
    assert restored == instance


@pytest.mark.parametrize("model_cls", _DEFAULT_CONSTRUCTABLE, ids=_DEFAULT_IDS)
def test_default_constructable_json_roundtrip(model_cls):
    """JSON-string roundtrip of default instance equals original."""
    instance = model_cls()
    j = instance.model_dump_json()
    restored = model_cls.model_validate_json(j)
    assert restored == instance


@pytest.mark.parametrize("model_cls", _MODELS, ids=_MODEL_IDS)
def test_validate_empty_dict_either_succeeds_or_raises_validation_error(model_cls):
    """Negative case: validating {} either succeeds (all-optional model) or
    raises ValidationError — never any other exception type."""
    try:
        model_cls.model_validate({})
    except ValidationError:
        pass  # expected for models with required fields
    except Exception as e:  # pragma: no cover — only fires on a real bug
        pytest.fail(
            f"{model_cls.__name__}.model_validate({{}}) raised "
            f"{type(e).__name__} instead of ValidationError: {e}"
        )
