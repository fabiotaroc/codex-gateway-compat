"""Response-side rewrites: restore namespaced tool calls and fix argument types.

Works on both Responses API SSE streams and non-streaming JSON bodies. Only
applied when the matching request was rewritten for a non-OpenAI model.

Also normalizes assistant-message `phase` and `output_item.done` order so Codex
Desktop can collapse the "Worked for" group. Non-OpenAI gateways often omit
`phase` and emit reasoning `.done` after the final message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from proxy.rewrite import FLAT_SEPARATOR, RequestContext

ARGUMENT_DELTA_EVENT = "response.function_call_arguments.delta"
ARGUMENT_DONE_EVENT = "response.function_call_arguments.done"
ITEM_ADDED_EVENT = "response.output_item.added"
ITEM_DONE_EVENT = "response.output_item.done"
ITEM_EVENTS = (ITEM_ADDED_EVENT, ITEM_DONE_EVENT)
RESPONSE_EVENTS = ("response.completed", "response.incomplete", "response.failed")
CALL_ITEM_TYPES = ("function_call", "tool_search_call", "custom_tool_call")
COMMENTARY_PHASE = "commentary"
FINAL_ANSWER_PHASE = "final_answer"


def coerce_integral_floats(value: Any) -> Tuple[Any, bool]:
    """Turn 100.0 into 100 recursively. Codex parses integer fields strictly."""
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 2**53:
            return int(value), True
        return value, False
    if isinstance(value, list):
        changed = False
        out = []
        for item in value:
            updated, item_changed = coerce_integral_floats(item)
            out.append(updated)
            changed = changed or item_changed
        return out, changed
    if isinstance(value, dict):
        changed = False
        out = {}
        for key, item in value.items():
            updated, item_changed = coerce_integral_floats(item)
            out[key] = updated
            changed = changed or item_changed
        return out, changed
    return value, False


def coerce_arguments(arguments: Any) -> Tuple[Any, bool]:
    """Arguments are usually a JSON string; tool_search uses an object."""
    if isinstance(arguments, str):
        if not arguments.strip():
            return arguments, False
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return arguments, False
        updated, changed = coerce_integral_floats(parsed)
        if not changed:
            return arguments, False
        return json.dumps(updated, ensure_ascii=False, separators=(",", ":")), True
    if isinstance(arguments, (dict, list)):
        return coerce_integral_floats(arguments)
    return arguments, False


def is_assistant_message(item: Any) -> bool:
    if not isinstance(item, dict) or item.get("type") != "message":
        return False
    return item.get("role") in (None, "assistant")


def is_unphased_assistant_message(item: Any) -> bool:
    return is_assistant_message(item) and not item.get("phase")


def tag_final_assistant_in_output(items: Any) -> Any:
    """Mark the last non-commentary assistant message as the final answer.

    If that message is followed only by reasoning items, move it after them so
    `response.output` matches the event order Codex Desktop expects.
    """
    if not isinstance(items, list):
        return items
    last = None
    for index, item in enumerate(items):
        if is_assistant_message(item) and item.get("phase") != COMMENTARY_PHASE:
            last = index
    if last is None:
        return items
    item = items[last]
    if not item.get("phase"):
        item["phase"] = FINAL_ANSWER_PHASE
    trailing = items[last + 1 :]
    if trailing and all(isinstance(extra, dict) and extra.get("type") == "reasoning" for extra in trailing):
        return items[:last] + trailing + [item]
    return items


def unflatten_name(name: Any, ctx: RequestContext) -> Optional[Tuple[str, str]]:
    if not isinstance(name, str) or not name:
        return None
    hit = ctx.mapping.get(name)
    if hit:
        return hit
    # Fallbacks for names the model invented from context rather than the
    # exact flat name: "<namespace>--<tool>" or "<namespace>.<tool>".
    for separator in (FLAT_SEPARATOR, "."):
        if separator in name:
            namespace, _, tool = name.partition(separator)
            if namespace in ctx.namespaces and tool:
                return namespace, tool
    return None


def rewrite_output_item(item: Any, ctx: RequestContext) -> Tuple[Any, bool]:
    if not isinstance(item, dict):
        return item, False
    changed = False
    item_type = item.get("type")
    if item_type == "function_call" and ctx.flatten:
        resolved = unflatten_name(item.get("name"), ctx)
        if resolved:
            item["namespace"], item["name"] = resolved
            changed = True
    if item_type in CALL_ITEM_TYPES and ctx.coerce_floats and "arguments" in item:
        item["arguments"], args_changed = coerce_arguments(item["arguments"])
        changed = changed or args_changed
    return item, changed


def rewrite_event(event_type: str, data: dict, ctx: RequestContext) -> List[dict]:
    """Return the list of event payloads to emit in place of `data`."""
    if event_type in ITEM_EVENTS and isinstance(data.get("item"), dict):
        data["item"], _ = rewrite_output_item(data["item"], ctx)
        return [data]

    if event_type in RESPONSE_EVENTS:
        response = data.get("response")
        if isinstance(response, dict) and isinstance(response.get("output"), list):
            response["output"] = tag_final_assistant_in_output(
                [rewrite_output_item(item, ctx)[0] for item in response["output"]]
            )
        return [data]

    if ctx.coerce_floats and event_type == ARGUMENT_DELTA_EVENT:
        # Raw deltas may carry "100.0" split across chunks; we cannot patch
        # them piecewise. Drop them and emit one corrected delta at .done.
        return []

    if ctx.coerce_floats and event_type == ARGUMENT_DONE_EVENT and "arguments" in data:
        data["arguments"], _ = coerce_arguments(data["arguments"])
        synthetic = dict(data)
        synthetic["type"] = ARGUMENT_DELTA_EVENT
        synthetic["delta"] = data["arguments"]
        synthetic.pop("arguments", None)
        return [synthetic, data]

    return [data]


def rewrite_json_response(raw: bytes, ctx: RequestContext) -> bytes:
    try:
        body = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(body, dict) or not isinstance(body.get("output"), list):
        return raw
    body["output"] = tag_final_assistant_in_output(
        [rewrite_output_item(item, ctx)[0] for item in body["output"]]
    )
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass
class _HeldEvent:
    event: dict
    had_event_line: bool
    other_lines: List[str]


@dataclass
class StreamState:
    """Holds an unphased assistant `output_item.done` until we know whether
    more work is coming or the turn is finished."""

    held: Optional[_HeldEvent] = field(default=None)

    def ingest(
        self,
        events: List[dict],
        had_event_line: bool,
        other_lines: List[str],
    ) -> List[_HeldEvent]:
        out: List[_HeldEvent] = []
        for event in events:
            current = _HeldEvent(event, had_event_line, other_lines)
            event_type = str(event.get("type") or "")
            item = event.get("item") if isinstance(event.get("item"), dict) else None

            if event_type == ITEM_DONE_EVENT and is_unphased_assistant_message(item):
                released = self._release(as_final=False)
                if released:
                    out.append(released)
                self.held = current
                continue

            if event_type == ITEM_ADDED_EVENT or event_type in RESPONSE_EVENTS:
                released = self._release(as_final=event_type in RESPONSE_EVENTS)
                if released:
                    out.append(released)
            elif event_type == ITEM_DONE_EVENT and item is not None and item.get("type") != "reasoning":
                released = self._release(as_final=False)
                if released:
                    out.append(released)

            out.append(current)
        return out

    def flush(self) -> List[_HeldEvent]:
        released = self._release(as_final=True)
        return [released] if released else []

    def _release(self, as_final: bool) -> Optional[_HeldEvent]:
        held = self.held
        self.held = None
        if held is None:
            return None
        if as_final:
            item = held.event.get("item")
            if is_unphased_assistant_message(item):
                item["phase"] = FINAL_ANSWER_PHASE
        return held


class SSERewriter:
    """Incremental Server-Sent Events rewriter. Emits each event as soon as
    its terminating blank line arrives, so streaming latency is preserved."""

    def __init__(self, ctx: RequestContext):
        self.ctx = ctx
        self._buffer = b""
        self._state = StreamState()

    def feed(self, chunk: bytes) -> bytes:
        self._buffer += chunk
        out: List[bytes] = []
        while True:
            boundary, length = self._find_boundary(self._buffer)
            if boundary < 0:
                break
            block = self._buffer[:boundary]
            self._buffer = self._buffer[boundary + length:]
            out.append(self._rewrite_block(block))
        return b"".join(out)

    def flush(self) -> bytes:
        parts: List[bytes] = []
        if self._buffer.strip():
            parts.append(self._rewrite_block(self._buffer))
            self._buffer = b""
        elif self._buffer:
            parts.append(self._buffer)
            self._buffer = b""
        parts.append(self._render(self._state.flush()))
        return b"".join(parts)

    @staticmethod
    def _find_boundary(buffer: bytes) -> Tuple[int, int]:
        lf = buffer.find(b"\n\n")
        crlf = buffer.find(b"\r\n\r\n")
        if lf < 0 and crlf < 0:
            return -1, 0
        if crlf >= 0 and (lf < 0 or crlf < lf):
            return crlf, 4
        return lf, 2

    def _rewrite_block(self, block: bytes) -> bytes:
        text = block.decode("utf-8", errors="surrogateescape")
        lines = text.replace("\r\n", "\n").split("\n")
        event_type: Optional[str] = None
        data_lines: List[str] = []
        other_lines: List[str] = []
        for line in lines:
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip(" "))
            else:
                other_lines.append(line)

        if not data_lines:
            return block + b"\n\n"

        payload_text = "\n".join(data_lines)
        try:
            payload = json.loads(payload_text)
        except ValueError:
            return block + b"\n\n"
        if not isinstance(payload, dict):
            return block + b"\n\n"

        resolved_type = event_type or str(payload.get("type") or "")
        events = rewrite_event(resolved_type, payload, self.ctx)
        for event in events:
            event.setdefault("type", resolved_type)
        return self._render(self._state.ingest(events, event_type is not None, other_lines))

    @staticmethod
    def _render(held_events: List[_HeldEvent]) -> bytes:
        rendered: List[bytes] = []
        for held in held_events:
            out_type = str(held.event.get("type") or "")
            parts = [line for line in held.other_lines if line]
            if held.had_event_line:
                parts.append("event: %s" % out_type)
            parts.append("data: %s" % json.dumps(held.event, ensure_ascii=False, separators=(",", ":")))
            rendered.append(("\n".join(parts) + "\n\n").encode("utf-8", errors="surrogateescape"))
        return b"".join(rendered)
