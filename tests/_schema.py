"""A JSON-Schema validator for exactly the keywords `tests/schemas/otlp-spans.schema.json` uses.

No dependency: `jsonschema` is not installed here and adding one for a snapshot would mean the snapshot is checked by a
library whose behaviour this project has not pinned. The subset is small and stated — `$ref` into `#/definitions`,
`type`, `required`, `properties`, `items`, `pattern`, `enum`, `minItems`, `minProperties`, `maxProperties`, `minLength` —
and anything outside it raises, so a schema that grows a keyword cannot be silently ignored. A validator that ignores what
it does not understand is the same failure as a verifier that reports a part it never checked.
"""
from __future__ import annotations

import re
from typing import Any

SUPPORTED = frozenset({"$schema", "$comment", "title", "definitions", "type", "required", "properties", "items",
                       "pattern", "enum", "minItems", "minProperties", "maxProperties", "minLength", "$ref"})

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}


class SchemaError(AssertionError):
    """Raised when an instance does not match — an `AssertionError`, so pytest shows it as a failed assertion."""


def _resolve(root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise SchemaError(f"only local refs are supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate(instance: Any, schema: dict[str, Any], *, root: dict[str, Any] | None = None, path: str = "$") -> None:
    root = root if root is not None else schema
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise SchemaError(f"{path}: schema keywords this validator does not implement: {sorted(unknown)}")
    if "$ref" in schema:
        validate(instance, _resolve(root, schema["$ref"]), root=root, path=path)
        return
    if "type" in schema:
        expected = _TYPES[schema["type"]]
        # bool is an int in Python; a JSON `true` is not a valid `integer`/`number`.
        if isinstance(instance, bool) and schema["type"] in ("integer", "number"):
            raise SchemaError(f"{path}: expected {schema['type']}, got boolean")
        if not isinstance(instance, expected):
            raise SchemaError(f"{path}: expected {schema['type']}, got {type(instance).__name__}")
    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{path}: {instance!r} is not one of {schema['enum']}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaError(f"{path}: shorter than {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            raise SchemaError(f"{path}: {instance!r} does not match {schema['pattern']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaError(f"{path}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for index, item in enumerate(instance):
                validate(item, schema["items"], root=root, path=f"{path}[{index}]")
    if isinstance(instance, dict):
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            raise SchemaError(f"{path}: fewer than {schema['minProperties']} properties")
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            raise SchemaError(f"{path}: more than {schema['maxProperties']} properties")
        for name in schema.get("required", []):
            if name not in instance:
                raise SchemaError(f"{path}: missing required property {name!r}")
        for name, sub in schema.get("properties", {}).items():
            if name in instance:
                validate(instance[name], sub, root=root, path=f"{path}.{name}")


__all__ = ["SUPPORTED", "SchemaError", "validate"]
