"""Strict validator for the JSON-schema subset used by the operator tool registry."""
from __future__ import annotations

from typing import Any

TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}


class SchemaError(ValueError):
    pass


def validate(value: Any, schema: dict, path: str = "$") -> None:
    t = schema.get("type")
    if t:
        py = TYPES.get(t)
        if py is None:
            raise SchemaError(f"unsupported schema type {t}")
        if not isinstance(value, py) or (t == "integer" and isinstance(value, bool)):
            raise SchemaError(f"{path}: expected {t}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: value not in {schema['enum']}")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise SchemaError(f"{path}: longer than {schema['maxLength']}")
    if t == "object":
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                raise SchemaError(f"{path}: missing required '{req}'")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise SchemaError(f"{path}: unexpected fields {sorted(extra)}")
        for k, v in value.items():
            if k in props:
                validate(v, props[k], f"{path}.{k}")
    if t == "array" and "items" in schema:
        for i, v in enumerate(value):
            validate(v, schema["items"], f"{path}[{i}]")
