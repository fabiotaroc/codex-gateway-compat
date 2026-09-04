"""Normalize Codex tool schemas for Meta Muse Spark's strict validator."""

from __future__ import annotations

import json
from typing import Any, Tuple

MUSE_MODEL_MARKERS = ("muse-spark",)
SCHEMA_KEYS = ("parameters", "input_schema", "inputSchema", "schema")
NESTED_OBJECT_KEYS = (
    "items",
    "additionalProperties",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "unevaluatedProperties",
    "unevaluatedItems",
    "propertyNames",
)
NESTED_ARRAY_KEYS = ("anyOf", "oneOf", "allOf", "prefixItems")
NESTED_MAP_KEYS = ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas")


def is_muse_model(model: Any) -> bool:
    text = str(model or "").lower()
    return any(marker in text for marker in MUSE_MODEL_MARKERS)


def _has_object_properties(schema: dict) -> bool:
    return isinstance(schema.get("properties"), dict) and bool(schema.get("properties"))


def _all_object_branches(branches: Any) -> bool:
    if not isinstance(branches, list) or not branches:
        return False
    for branch in branches:
        if not isinstance(branch, dict):
            return False
        if branch.get("type") == "object" or _has_object_properties(branch):
            continue
        return False
    return True


def _flatten_object_combinator(schema: dict, key: str) -> int:
    branches = schema.get(key)
    if not _all_object_branches(branches):
        return 0
    if schema.get("type") == "object" and _has_object_properties(schema):
        return 0

    merged = {}
    for branch in branches:
        props = branch.get("properties")
        if isinstance(props, dict):
            for name, value in props.items():
                merged.setdefault(name, value)

    schema["type"] = "object"
    schema["additionalProperties"] = False
    if merged:
        existing = schema.get("properties")
        if isinstance(existing, dict):
            for name, value in merged.items():
                existing.setdefault(name, value)
            schema["properties"] = existing
        else:
            schema["properties"] = merged
    schema.pop(key, None)
    return 1


def normalize_schema(schema: Any, flatten_root_combinators: bool = False) -> Tuple[Any, int]:
    """Make a JSON Schema acceptable to strict OpenAI-compatible validators.

    Flatten `oneOf` only at a tool parameters root. Nested `anyOf` unions
    (for example string|null) must be left intact.
    """
    patched = 0

    if isinstance(schema, list):
        out = []
        for item in schema:
            updated, count = normalize_schema(item, flatten_root_combinators=False)
            out.append(updated)
            patched += count
        return out, patched

    if not isinstance(schema, dict):
        return schema, 0

    if "$ref" in schema and "type" in schema:
        schema.pop("type", None)
        patched += 1

    for key in NESTED_OBJECT_KEYS:
        child = schema.get(key)
        if isinstance(child, dict):
            schema[key], count = normalize_schema(child, flatten_root_combinators=False)
            patched += count

    for key in NESTED_ARRAY_KEYS:
        child = schema.get(key)
        if isinstance(child, list):
            schema[key], count = normalize_schema(child, flatten_root_combinators=False)
            patched += count

    for key in NESTED_MAP_KEYS:
        child = schema.get(key)
        if isinstance(child, dict):
            updated = {}
            for name, value in child.items():
                updated[name], count = normalize_schema(value, flatten_root_combinators=False)
                patched += count
            schema[key] = updated

    if flatten_root_combinators and isinstance(schema.get("oneOf"), list) and schema.get("type") in (None, "null"):
        patched += _flatten_object_combinator(schema, "oneOf")

    properties = schema.get("properties")
    if "$ref" not in schema and _has_object_properties(schema):
        if schema.get("type") in (None, "null"):
            schema["type"] = "object"
            patched += 1
        extra = schema.get("additionalProperties")
        if extra is not False:
            schema["additionalProperties"] = False
            patched += 1
        keys = list(properties.keys())
        required = schema.get("required")
        if not isinstance(required, list):
            schema["required"] = keys
            patched += 1
        else:
            missing = [key for key in keys if key not in required]
            if missing:
                schema["required"] = list(required) + missing
                patched += 1

    return schema, patched


def complete_required(schema: Any) -> Tuple[Any, int]:
    return normalize_schema(schema, flatten_root_combinators=True)


def rewrite_tool_node(node: Any) -> int:
    patched = 0
    if isinstance(node, list):
        for item in node:
            patched += rewrite_tool_node(item)
        return patched
    if not isinstance(node, dict):
        return 0

    for key in SCHEMA_KEYS:
        child = node.get(key)
        if isinstance(child, dict):
            node[key], count = normalize_schema(child, flatten_root_combinators=True)
            patched += count

    function = node.get("function")
    if isinstance(function, dict):
        patched += rewrite_tool_node(function)

    tools = node.get("tools")
    if tools is not None:
        patched += rewrite_tool_node(tools)

    return patched


def rewrite_request_body(body: Any) -> Tuple[Any, int]:
    if not isinstance(body, dict):
        return body, 0
    if not is_muse_model(body.get("model")):
        return body, 0
    patched = 0
    if "tools" in body:
        patched += rewrite_tool_node(body["tools"])
    functions = body.get("functions")
    if functions is not None:
        patched += rewrite_tool_node(functions)
    return body, patched


def rewrite_request_bytes(raw: bytes) -> Tuple[bytes, int]:
    if not raw:
        return raw, 0
    try:
        body = json.loads(raw)
    except ValueError:
        return raw, 0
    updated, patched = rewrite_request_body(body)
    if patched == 0:
        return raw, 0
    return json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), patched


def summarize_models(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("model") or "")
    return ""


def summarize_tools(body: Any) -> Any:
    if not isinstance(body, dict):
        return []
    names = []
    schemas = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            name = node.get("name")
            params = None
            for key in SCHEMA_KEYS:
                if isinstance(node.get(key), dict):
                    params = node[key]
                    break
            if name and params is not None:
                names.append(name)
                schemas.append(
                    {
                        "name": name,
                        "type": node.get("type"),
                        "strict": node.get("strict"),
                        "parameter_keys": list(params.keys()),
                        "has_oneOf": "oneOf" in params,
                        "has_anyOf": "anyOf" in params,
                        "type_field": params.get("type"),
                        "required": params.get("required"),
                    }
                )
            if "function" in node:
                walk(node["function"])
            if "tools" in node:
                walk(node["tools"])

    walk(body.get("tools"))
    walk(body.get("functions"))
    return {"names": names, "schemas": schemas}
