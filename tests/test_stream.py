#!/usr/bin/env python3
import json
import unittest

from proxy.rewrite import FLAT_SEPARATOR, RequestContext, prepare_request
from proxy.stream import (
    SSERewriter,
    coerce_arguments,
    coerce_integral_floats,
    rewrite_json_response,
    rewrite_output_item,
    unflatten_name,
)


def _ctx():
    ctx = RequestContext(model="meta/muse-spark-1.3-contributor", muse=True, flatten=True, coerce_floats=True)
    ctx.mapping["mcp__codex_apps__notion%s_fetch" % FLAT_SEPARATOR] = ("mcp__codex_apps__notion", "_fetch")
    ctx.namespaces.add("mcp__codex_apps__notion")
    return ctx


def _event(event_type, payload):
    payload = dict(payload, type=event_type)
    return ("event: %s\ndata: %s\n\n" % (event_type, json.dumps(payload))).encode()


def _parse_events(raw):
    events = []
    for block in raw.decode().split("\n\n"):
        if not block.strip():
            continue
        data = [line[5:].strip() for line in block.split("\n") if line.startswith("data:")]
        events.append(json.loads("\n".join(data)))
    return events


class CoercionTests(unittest.TestCase):
    def test_integral_floats_become_ints_recursively(self):
        value, changed = coerce_integral_floats({"a": 100.0, "b": [1.0, 2.5, {"c": -3.0}], "d": "x", "e": True})
        self.assertTrue(changed)
        self.assertEqual(value, {"a": 100, "b": [1, 2.5, {"c": -3}], "d": "x", "e": True})
        self.assertIsInstance(value["a"], int)
        self.assertIsInstance(value["b"][1], float)

    def test_string_arguments_are_rewritten_only_when_needed(self):
        args, changed = coerce_arguments('{"cmd":"echo test","max_output_tokens":100.0,"yield_time_ms":5000.0}')
        self.assertTrue(changed)
        self.assertEqual(json.loads(args), {"cmd": "echo test", "max_output_tokens": 100, "yield_time_ms": 5000})
        same, changed = coerce_arguments('{"cmd":"ls"}')
        self.assertFalse(changed)
        self.assertEqual(same, '{"cmd":"ls"}')
        broken, changed = coerce_arguments('{"cmd": ')
        self.assertFalse(changed)
        self.assertEqual(broken, '{"cmd": ')
        empty, changed = coerce_arguments("")
        self.assertFalse(changed)


class UnflattenTests(unittest.TestCase):
    def test_exact_mapping_and_fallbacks(self):
        ctx = _ctx()
        self.assertEqual(
            unflatten_name("mcp__codex_apps__notion%s_fetch" % FLAT_SEPARATOR, ctx),
            ("mcp__codex_apps__notion", "_fetch"),
        )
        # Model improvised a sibling tool name with the separator.
        self.assertEqual(
            unflatten_name("mcp__codex_apps__notion%ssearch" % FLAT_SEPARATOR, ctx),
            ("mcp__codex_apps__notion", "search"),
        )
        # Muse's own habit: dotted namespace.tool.
        self.assertEqual(unflatten_name("mcp__codex_apps__notion._fetch", ctx), ("mcp__codex_apps__notion", "_fetch"))
        self.assertIsNone(unflatten_name("exec_command", ctx))
        self.assertIsNone(unflatten_name("unknown--thing", ctx))

    def test_rewrite_function_call_item(self):
        ctx = _ctx()
        item = {
            "type": "function_call",
            "name": "mcp__codex_apps__notion%s_fetch" % FLAT_SEPARATOR,
            "arguments": '{"id":"abc","limit":10.0}',
            "call_id": "c1",
        }
        updated, changed = rewrite_output_item(item, ctx)
        self.assertTrue(changed)
        self.assertEqual(updated["namespace"], "mcp__codex_apps__notion")
        self.assertEqual(updated["name"], "_fetch")
        self.assertEqual(json.loads(updated["arguments"]), {"id": "abc", "limit": 10})

    def test_tool_search_call_arguments_object(self):
        ctx = _ctx()
        item = {"type": "tool_search_call", "arguments": {"limit": 8.0, "query": "notion"}}
        updated, changed = rewrite_output_item(item, ctx)
        self.assertTrue(changed)
        self.assertEqual(updated["arguments"], {"limit": 8, "query": "notion"})

    def test_no_op_when_context_is_openai(self):
        ctx = RequestContext(model="openai/gpt-5.6-luna")
        item = {"type": "function_call", "name": "a--b", "arguments": '{"x":1.0}'}
        updated, changed = rewrite_output_item(dict(item), ctx)
        self.assertFalse(changed)
        self.assertEqual(updated, item)


class SSERewriterTests(unittest.TestCase):
    def test_rewrites_items_across_chunk_boundaries(self):
        ctx = _ctx()
        flat = "mcp__codex_apps__notion%s_fetch" % FLAT_SEPARATOR
        stream = b"".join(
            [
                _event("response.created", {"response": {"id": "r1"}}),
                _event(
                    "response.output_item.added",
                    {"output_index": 0, "item": {"type": "function_call", "name": flat, "arguments": "", "call_id": "c1"}},
                ),
                _event("response.function_call_arguments.delta", {"item_id": "fc1", "output_index": 0, "delta": '{"id":"a","limit":1'}),
                _event("response.function_call_arguments.delta", {"item_id": "fc1", "output_index": 0, "delta": "0.0}"}),
                _event("response.function_call_arguments.done", {"item_id": "fc1", "output_index": 0, "arguments": '{"id":"a","limit":10.0}'}),
                _event(
                    "response.output_item.done",
                    {"output_index": 0, "item": {"type": "function_call", "name": flat, "arguments": '{"id":"a","limit":10.0}', "call_id": "c1"}},
                ),
                _event(
                    "response.completed",
                    {"response": {"id": "r1", "output": [{"type": "function_call", "name": flat, "arguments": '{"id":"a","limit":10.0}', "call_id": "c1"}]}},
                ),
            ]
        )
        rewriter = SSERewriter(ctx)
        out = b""
        # Feed in awkward 7-byte pieces to exercise buffering.
        for i in range(0, len(stream), 7):
            out += rewriter.feed(stream[i : i + 7])
        out += rewriter.flush()

        events = _parse_events(out)
        types = [event["type"] for event in events]
        self.assertEqual(
            types,
            [
                "response.created",
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        added = events[1]["item"]
        self.assertEqual((added["namespace"], added["name"]), ("mcp__codex_apps__notion", "_fetch"))
        delta = events[2]
        self.assertEqual(json.loads(delta["delta"]), {"id": "a", "limit": 10})
        self.assertNotIn("arguments", delta)
        self.assertEqual(json.loads(events[3]["arguments"]), {"id": "a", "limit": 10})
        done = events[4]["item"]
        self.assertEqual(done["namespace"], "mcp__codex_apps__notion")
        self.assertEqual(json.loads(done["arguments"])["limit"], 10)
        completed = events[5]["response"]["output"][0]
        self.assertEqual(completed["name"], "_fetch")
        self.assertIn(b"event: response.output_item.done\ndata: ", out)

    def test_passes_through_non_json_and_crlf_blocks(self):
        rewriter = SSERewriter(_ctx())
        out = rewriter.feed(b": keep-alive\r\n\r\ndata: [DONE]\r\n\r\n")
        out += rewriter.flush()
        self.assertEqual(out, b": keep-alive\n\ndata: [DONE]\n\n")

    def test_plain_text_events_untouched(self):
        rewriter = SSERewriter(_ctx())
        raw = b'data: {"type":"response.output_text.delta","delta":"hi 1.0"}\n\n'
        self.assertEqual(rewriter.feed(raw), raw)

    def test_holds_unphased_message_until_after_trailing_reasoning(self):
        rewriter = SSERewriter(_ctx())
        message = {"type": "message", "role": "assistant", "id": "m1", "content": [{"type": "output_text", "text": "hello"}]}
        reasoning = {"type": "reasoning", "id": "r1", "summary": [{"type": "summary_text", "text": ""}]}
        prefix = rewriter.feed(
            b"".join(
                [
                    _event("response.created", {"response": {"id": "resp"}}),
                    _event("response.output_item.added", {"output_index": 0, "item": reasoning}),
                    _event("response.output_item.added", {"output_index": 1, "item": message}),
                    _event("response.output_text.delta", {"delta": "hello"}),
                    _event("response.output_item.done", {"output_index": 1, "item": message}),
                ]
            )
        )
        types = [event["type"] for event in _parse_events(prefix)]
        self.assertEqual(
            types,
            [
                "response.created",
                "response.output_item.added",
                "response.output_item.added",
                "response.output_text.delta",
            ],
        )

        mid = rewriter.feed(_event("response.output_item.done", {"output_index": 0, "item": reasoning}))
        mid_events = _parse_events(mid)
        self.assertEqual(len(mid_events), 1)
        self.assertEqual(mid_events[0]["item"]["type"], "reasoning")

        tail = rewriter.feed(
            _event(
                "response.completed",
                {"response": {"id": "resp", "output": [reasoning, dict(message)]}},
            )
        )
        tail += rewriter.flush()
        tail_events = _parse_events(tail)
        self.assertEqual([event["type"] for event in tail_events], ["response.output_item.done", "response.completed"])
        self.assertEqual(tail_events[0]["item"]["type"], "message")
        self.assertEqual(tail_events[0]["item"]["phase"], "final_answer")
        self.assertEqual(tail_events[1]["response"]["output"][-1]["type"], "message")
        self.assertEqual(tail_events[1]["response"]["output"][-1]["phase"], "final_answer")
        self.assertEqual([item["type"] for item in tail_events[1]["response"]["output"]], ["reasoning", "message"])

    def test_releases_unphased_message_as_commentary_when_more_work_starts(self):
        rewriter = SSERewriter(_ctx())
        message = {"type": "message", "role": "assistant", "id": "m1", "content": [{"type": "output_text", "text": "checking"}]}
        call = {"type": "function_call", "name": "exec_command", "arguments": "", "call_id": "c1"}
        out = rewriter.feed(
            b"".join(
                [
                    _event("response.output_item.done", {"output_index": 0, "item": message}),
                    _event("response.output_item.added", {"output_index": 1, "item": call}),
                ]
            )
        )
        events = _parse_events(out)
        self.assertEqual([event["type"] for event in events], ["response.output_item.done", "response.output_item.added"])
        self.assertNotIn("phase", events[0]["item"])
        self.assertEqual(events[1]["item"]["type"], "function_call")

    def test_commentary_messages_are_not_held(self):
        rewriter = SSERewriter(_ctx())
        message = {
            "type": "message",
            "role": "assistant",
            "id": "m1",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "working"}],
        }
        out = rewriter.feed(_event("response.output_item.done", {"output_index": 0, "item": message}))
        events = _parse_events(out)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["item"]["phase"], "commentary")

    def test_flush_tags_held_message_when_stream_ends(self):
        rewriter = SSERewriter(_ctx())
        message = {"type": "message", "role": "assistant", "id": "m1", "content": [{"type": "output_text", "text": "hello"}]}
        self.assertEqual(rewriter.feed(_event("response.output_item.done", {"output_index": 0, "item": message})), b"")
        events = _parse_events(rewriter.flush())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["item"]["phase"], "final_answer")


class JSONResponseTests(unittest.TestCase):
    def test_rewrites_non_streaming_output(self):
        body = {"id": "r", "output": [{"type": "function_call", "name": "mcp__codex_apps__notion--_fetch", "arguments": '{"n":2.0}'}]}
        out = json.loads(rewrite_json_response(json.dumps(body).encode(), _ctx()))
        self.assertEqual(out["output"][0]["namespace"], "mcp__codex_apps__notion")
        self.assertEqual(out["output"][0]["name"], "_fetch")
        self.assertEqual(json.loads(out["output"][0]["arguments"]), {"n": 2})

    def test_tags_and_reorders_final_assistant_message(self):
        body = {
            "id": "r",
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
                {"type": "reasoning", "id": "rs1", "summary": []},
            ],
        }
        out = json.loads(rewrite_json_response(json.dumps(body).encode(), _ctx()))
        self.assertEqual([item["type"] for item in out["output"]], ["reasoning", "message"])
        self.assertEqual(out["output"][1]["phase"], "final_answer")

    def test_round_trip_through_prepare_request(self):
        body = {
            "model": "meta/muse-spark-1.3",
            "tools": [{"type": "namespace", "name": "mcp__x", "tools": [{"name": "run", "parameters": {"type": "object", "properties": {}}}]}],
        }
        updated, ctx, _ = prepare_request(body)
        flat = updated["tools"][0]["name"]
        item, changed = rewrite_output_item({"type": "function_call", "name": flat, "arguments": "{}"}, ctx)
        self.assertTrue(changed)
        self.assertEqual((item["namespace"], item["name"]), ("mcp__x", "run"))


if __name__ == "__main__":
    unittest.main()
