"""Request-side rewrites that make Codex Desktop usable with non-OpenAI models.

Three independent concerns, gated per model:

* Muse Spark only: normalize tool JSON Schemas for Meta's strict validator and
  drop `custom` (grammar) tools, which Meta rejects outright.
* Every non-OpenAI model: flatten `type: "namespace"` tool wrappers into plain
  functions, both in `tools[]` and inside `tool_search_output` history items,
  and rename namespaced `function_call` history items to match. The response
  side (see stream.py) reverses the mapping so Codex still sees namespaced calls.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

MUSE_MODEL_MARKERS = ("muse-spark",)
OPENAI_MODEL_PREFIXES = ("openai/", "gpt-", "o1", "o3", "o4", "codex")
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
UNSUPPORTED_TOOL_TYPES = ("custom",)

# Joins namespace and tool name into one flat function name. Hyphens are legal
# in function names and never appear in Codex namespace identifiers.
FLAT_SEPARATOR = "--"
MAX_FUNCTION_NAME = 64


@dataclass
class RequestContext:
    model: str = ""
    muse: bool = False
    flatten: bool = False
    coerce_floats: bool = False
    mapping: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    namespaces: Set[str] = field(default_factory=set)

    @property
    def rewrites_responses(self) -> bool:
        return self.flatten or self.coerce_floats


def is_muse_model(model: Any) -> bool:
    text = str(model or "").lower()
    return any(marker in text for marker in MUSE_MODEL_MARKERS)


def is_openai_model(model: Any) -> bool:
    text = str(model or "").lower()
    if not text:
        # Unknown model: leave the request alone.
        return True
    return text.startswith(OPENAI_MODEL_PREFIXES)


def build_context(model: Any) -> RequestContext:
    openai = is_openai_model(model)
    return RequestContext(
        model=str(model or ""),
        muse=is_muse_model(model),
        flatten=not openai,
        coerce_floats=not openai,
    )


# --------------------------------------------------------------------------- #
# Strict JSON Schema normalization (Muse Spark)
# --------------------------------------------------------------------------- #


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

    merged: Dict[str, Any] = {}
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

    if flatten_root_combinators and isinstance(schema.get("oneOf"), list):
        if schema.get("type") in (None, "null") or not _has_object_properties(schema):
            patched += _flatten_object_combinator(schema, "oneOf")

    properties = schema.get("properties")
    if "$ref" not in schema and _has_object_properties(schema):
        if schema.get("type") in (None, "null"):
            schema["type"] = "object"
            patched += 1
        if schema.get("additionalProperties") is not False:
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


def strip_unsupported_tools(tools: Any) -> Tuple[Any, int]:
    if not isinstance(tools, list):
        return tools, 0
    kept = []
    removed = 0
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") in UNSUPPORTED_TOOL_TYPES:
            removed += 1
            continue
        if isinstance(tool, dict) and "tools" in tool:
            tool["tools"], child_removed = strip_unsupported_tools(tool["tools"])
            removed += child_removed
        kept.append(tool)
    return kept, removed


# --------------------------------------------------------------------------- #
# Namespace flattening (all non-OpenAI models)
# --------------------------------------------------------------------------- #


def flat_name_for(namespace: str, name: str, ctx: RequestContext) -> str:
    flat = "%s%s%s" % (namespace, FLAT_SEPARATOR, name)
    if len(flat) > MAX_FUNCTION_NAME:
        digest = hashlib.sha1(("%s/%s" % (namespace, name)).encode("utf-8")).hexdigest()[:8]
        prefix = "ns_%s%s" % (digest, FLAT_SEPARATOR)
        flat = (prefix + name)[:MAX_FUNCTION_NAME]
    ctx.mapping[flat] = (namespace, name)
    ctx.namespaces.add(namespace)
    return flat


def _flatten_namespace(namespace_tool: dict, ctx: RequestContext) -> List[dict]:
    namespace = str(namespace_tool.get("name") or "")
    ns_description = str(namespace_tool.get("description") or "").strip()
    flattened = []
    for child in namespace_tool.get("tools") or []:
        if not isinstance(child, dict):
            continue
        child_name = str(child.get("name") or "")
        if not child_name:
            continue
        flat = dict(child)
        flat["type"] = "function"
        flat["name"] = flat_name_for(namespace, child_name, ctx)
        flat.pop("defer_loading", None)
        parts = []
        if namespace:
            header = "[%s]" % namespace
            if ns_description:
                header = "%s %s" % (header, ns_description)
            parts.append(header)
        child_description = str(child.get("description") or "").strip()
        if child_description:
            parts.append(child_description)
        if parts:
            flat["description"] = "\n\n".join(parts)
        flattened.append(flat)
    return flattened


def flatten_tools(tools: Any, ctx: RequestContext) -> Tuple[Any, int]:
    if not isinstance(tools, list):
        return tools, 0
    out = []
    count = 0
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            out.extend(_flatten_namespace(tool, ctx))
            count += 1
        else:
            out.append(tool)
    return out, count


def flatten_input(items: Any, ctx: RequestContext) -> Tuple[int, List[dict]]:
    """Flatten namespaced tool references inside the conversation history.

    Returns the patch count and every tool definition found in
    `tool_search_output` items, so callers can promote them to `tools[]`.
    """
    if not isinstance(items, list):
        return 0, []
    count = 0
    discovered: List[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "tool_search_output" and isinstance(item.get("tools"), list):
            item["tools"], flattened = flatten_tools(item["tools"], ctx)
            count += flattened
            discovered.extend(tool for tool in item["tools"] if isinstance(tool, dict))
        elif item_type in ("function_call", "function_call_output"):
            namespace = item.get("namespace")
            name = item.get("name")
            if namespace and name:
                item["name"] = flat_name_for(str(namespace), str(name), ctx)
                item.pop("namespace", None)
                count += 1
    return count, discovered


def promote_discovered_tools(tools: Any, discovered: List[dict]) -> Tuple[Any, int]:
    """Append tools found via tool_search to the top-level tool list.

    OpenAI models treat definitions inside `tool_search_output` as callable;
    other models only call what is in `tools[]`. Later discoveries win.
    """
    if not discovered:
        return tools, 0
    base = list(tools) if isinstance(tools, list) else []
    existing = {tool.get("name") for tool in base if isinstance(tool, dict)}
    added = 0
    for tool in reversed(discovered):
        name = tool.get("name")
        if not name or name in existing or tool.get("type") not in (None, "function"):
            continue
        promoted = dict(tool)
        promoted.pop("defer_loading", None)
        promoted.setdefault("type", "function")
        base.append(promoted)
        existing.add(name)
        added += 1
    return base, added


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def prepare_request(body: Any) -> Tuple[Any, RequestContext, int]:
    if not isinstance(body, dict):
        return body, RequestContext(), 0
    ctx = build_context(body.get("model"))
    patched = 0

    if ctx.flatten:
        if "tools" in body:
            body["tools"], count = flatten_tools(body["tools"], ctx)
            patched += count
        count, discovered = flatten_input(body.get("input"), ctx)
        patched += count
        if discovered:
            body["tools"], count = promote_discovered_tools(body.get("tools"), discovered)
            patched += count

    if ctx.muse:
        if "tools" in body:
            body["tools"], removed = strip_unsupported_tools(body["tools"])
            patched += removed
            patched += rewrite_tool_node(body["tools"])
        functions = body.get("functions")
        if functions is not None:
            patched += rewrite_tool_node(functions)

    return body, ctx, patched


def rewrite_request_body(body: Any) -> Tuple[Any, int]:
    updated, _, patched = prepare_request(body)
    return updated, patched


def rewrite_request_bytes(raw: bytes) -> Tuple[bytes, int, RequestContext]:
    if not raw:
        return raw, 0, RequestContext()
    try:
        body = json.loads(raw)
    except ValueError:
        return raw, 0, RequestContext()
    updated, ctx, patched = prepare_request(body)
    if patched == 0:
        return raw, 0, ctx
    encoded = json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, patched, ctx


def summarize_models(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("model") or "")
    return ""


def summarize_tools(body: Any) -> Any:
    if not isinstance(body, dict):
        return []
    names = []
    schemas = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            name = node.get("name")
            params: Optional[dict] = None
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
