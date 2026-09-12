#!/usr/bin/env python3
import json
import unittest

from proxy.rewrite import (
    FLAT_SEPARATOR,
    complete_required,
    is_muse_model,
    is_openai_model,
    prepare_request,
    rewrite_request_body,
    rewrite_request_bytes,
)


class MuseModelTests(unittest.TestCase):
    def test_matches_contributor_and_paid(self):
        self.assertTrue(is_muse_model("meta/muse-spark-1.3-contributor"))
        self.assertTrue(is_muse_model("meta/muse-spark-1.3"))
        self.assertFalse(is_muse_model("openai/gpt-5.5"))
        self.assertFalse(is_muse_model("deepseek-v4-flash"))

    def test_openai_detection(self):
        self.assertTrue(is_openai_model("openai/gpt-5.6-luna"))
        self.assertTrue(is_openai_model("gpt-5.5"))
        self.assertTrue(is_openai_model(""))
        self.assertFalse(is_openai_model("meta/muse-spark-1.3-contributor"))
        self.assertFalse(is_openai_model("google/gemini-3.5-pro"))
        self.assertFalse(is_openai_model("anthropic/claude-opus-5"))


class CompleteRequiredTests(unittest.TestCase):
    def test_adds_missing_required_when_additional_properties_false(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        }
        updated, patched = complete_required(schema)
        self.assertEqual(patched, 1)
        self.assertEqual(updated["required"], ["limit"])

    def test_appends_only_missing_keys(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        }
        updated, patched = complete_required(schema)
        self.assertEqual(patched, 1)
        self.assertEqual(updated["required"], ["query", "limit"])

    def test_leaves_complete_required_alone(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"limit": {"type": "integer"}},
            "required": ["limit"],
        }
        _, patched = complete_required(schema)
        self.assertEqual(patched, 0)

    def test_tightens_object_schemas_that_allow_extra_properties(self):
        schema = {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        }
        updated, patched = complete_required(schema)
        self.assertGreaterEqual(patched, 1)
        self.assertEqual(updated["additionalProperties"], False)
        self.assertEqual(updated["required"], ["limit"])

    def test_flattens_root_oneof_without_type(self):
        schema = {
            "$defs": {"stringValue": {"type": "string"}},
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "mode": {"type": "string", "enum": ["view"]},
                        "id": {"type": "string"},
                    },
                    "required": ["mode", "id"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "mode": {"type": "string", "enum": ["delete"]},
                        "id": {"type": "string"},
                    },
                    "required": ["mode", "id"],
                },
            ],
        }
        updated, patched = complete_required(schema)
        self.assertGreaterEqual(patched, 1)
        self.assertEqual(updated["type"], "object")
        self.assertNotIn("oneOf", updated)
        self.assertEqual(updated["additionalProperties"], False)
        self.assertIn("mode", updated["properties"])
        self.assertIn("id", updated["required"])

    def test_preserves_nested_nullable_anyof(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "limit": {"type": "integer"},
            },
            "required": ["id", "limit"],
        }
        updated, patched = complete_required(schema)
        self.assertEqual(patched, 0)
        self.assertEqual(
            updated["properties"]["id"],
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
        )

    def test_drops_type_sibling_of_ref(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"$ref": "#/$defs/stringValue", "type": "string"},
            },
            "required": ["id"],
        }
        updated, patched = complete_required(schema)
        self.assertGreaterEqual(patched, 1)
        self.assertEqual(updated["properties"]["id"], {"$ref": "#/$defs/stringValue"})

    def test_patches_nested_object_properties(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "filter": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"limit": {"type": "integer"}},
                }
            },
            "required": ["filter"],
        }
        updated, patched = complete_required(schema)
        self.assertEqual(patched, 1)
        self.assertEqual(updated["properties"]["filter"]["required"], ["limit"])


class RequestRewriteTests(unittest.TestCase):
    def _list_threads_request(self, model):
        return {
            "model": model,
            "tools": [
                {
                    "type": "function",
                    "name": "list_threads",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"limit": {"type": "integer"}},
                    },
                }
            ],
        }

    def test_rewrites_muse_spark_only(self):
        muse, muse_count = rewrite_request_body(self._list_threads_request("meta/muse-spark-1.3-contributor"))
        other, other_count = rewrite_request_body(self._list_threads_request("openai/gpt-5.5"))
        self.assertEqual(muse_count, 1)
        self.assertEqual(muse["tools"][0]["parameters"]["required"], ["limit"])
        self.assertEqual(other_count, 0)
        self.assertNotIn("required", other["tools"][0]["parameters"])

    def _namespaced_request(self, model):
        return {
            "model": model,
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__codex_app",
                    "description": "Codex app tools.",
                    "tools": [
                        {
                            "name": "list_archived_threads",
                            "description": "List archived threads.",
                            "strict": False,
                            "parameters": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"limit": {"type": "integer"}},
                            },
                        }
                    ],
                },
                {"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {}}},
            ],
        }

    def test_flattens_namespaces_for_muse_and_normalizes_schema(self):
        updated, ctx, patched = prepare_request(self._namespaced_request("meta/muse-spark-1.3"))
        self.assertGreaterEqual(patched, 2)
        names = [tool["name"] for tool in updated["tools"]]
        flat = "mcp__codex_app%slist_archived_threads" % FLAT_SEPARATOR
        self.assertEqual(names, [flat, "exec_command"])
        tool = updated["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertNotIn("defer_loading", tool)
        self.assertIn("[mcp__codex_app] Codex app tools.", tool["description"])
        self.assertIn("List archived threads.", tool["description"])
        self.assertEqual(tool["parameters"]["required"], ["limit"])
        self.assertEqual(ctx.mapping[flat], ("mcp__codex_app", "list_archived_threads"))
        self.assertTrue(ctx.rewrites_responses)

    def test_flattens_namespaces_for_other_non_openai_models_without_schema_changes(self):
        updated, ctx, patched = prepare_request(self._namespaced_request("google/gemini-3.5-pro"))
        self.assertEqual(patched, 1)
        self.assertEqual(updated["tools"][0]["type"], "function")
        # Not Muse: strict-schema normalization must not run.
        self.assertNotIn("required", updated["tools"][0]["parameters"])
        self.assertTrue(ctx.flatten)
        self.assertFalse(ctx.muse)

    def test_leaves_namespaces_alone_for_openai(self):
        updated, ctx, patched = prepare_request(self._namespaced_request("openai/gpt-5.6-luna"))
        self.assertEqual(patched, 0)
        self.assertEqual(updated["tools"][0]["type"], "namespace")
        self.assertFalse(ctx.rewrites_responses)

    def test_flattens_tool_search_output_and_history_calls(self):
        body = {
            "model": "meta/muse-spark-1.3-contributor",
            "tools": [],
            "input": [
                {
                    "type": "tool_search_output",
                    "call_id": "c1",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "mcp__codex_apps__notion",
                            "tools": [
                                {"name": "_fetch", "defer_loading": True, "parameters": {"type": "object", "properties": {}}}
                            ],
                        }
                    ],
                },
                {"type": "function_call", "name": "js", "namespace": "mcp__cua_repl", "arguments": "{}", "call_id": "c2"},
                {"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "c3"},
            ],
        }
        updated, ctx, patched = prepare_request(body)
        self.assertGreaterEqual(patched, 2)
        searched = updated["input"][0]["tools"][0]
        self.assertEqual(searched["type"], "function")
        self.assertEqual(searched["name"], "mcp__codex_apps__notion%s_fetch" % FLAT_SEPARATOR)
        self.assertNotIn("defer_loading", searched)
        history = updated["input"][1]
        self.assertEqual(history["name"], "mcp__cua_repl%sjs" % FLAT_SEPARATOR)
        self.assertNotIn("namespace", history)
        self.assertEqual(updated["input"][2]["name"], "exec_command")
        self.assertEqual(ctx.mapping[history["name"]], ("mcp__cua_repl", "js"))
        self.assertIn("mcp__codex_apps__notion", ctx.namespaces)
        # Discovered tools are promoted to the top-level list so Muse can call them.
        promoted = [tool["name"] for tool in updated["tools"]]
        self.assertEqual(promoted, [searched["name"]])
        self.assertNotIn("defer_loading", updated["tools"][0])
        self.assertEqual(updated["tools"][0]["type"], "function")

    def test_promotion_dedupes_and_prefers_latest_definition(self):
        def tso(desc):
            return {
                "type": "tool_search_output",
                "tools": [{"type": "namespace", "name": "mcp__n", "tools": [{"name": "t", "description": desc, "parameters": {"type": "object", "properties": {}}}]}],
            }

        body = {
            "model": "google/gemini-3.5-pro",
            "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {}}}],
            "input": [tso("old"), tso("new")],
        }
        updated, _, _ = prepare_request(body)
        names = [tool["name"] for tool in updated["tools"]]
        self.assertEqual(names, ["exec_command", "mcp__n%st" % FLAT_SEPARATOR])
        self.assertIn("new", updated["tools"][1]["description"])

    def test_long_flat_names_stay_within_limit_and_map_back(self):
        namespace = "mcp__codex_apps__notion"
        name = "_notion_show_advanced_analysis_next_steps_and_more_words_here"
        body = {
            "model": "meta/muse-spark-1.3",
            "tools": [{"type": "namespace", "name": namespace, "tools": [{"name": name, "parameters": {"type": "object", "properties": {}}}]}],
        }
        updated, ctx, _ = prepare_request(body)
        flat = updated["tools"][0]["name"]
        self.assertLessEqual(len(flat), 64)
        self.assertEqual(ctx.mapping[flat], (namespace, name))

    def test_bytes_round_trip_leaves_non_json_alone(self):
        raw, patched, ctx = rewrite_request_bytes(b"not-json")
        self.assertEqual(raw, b"not-json")
        self.assertEqual(patched, 0)
        self.assertFalse(ctx.rewrites_responses)

    def test_strips_custom_apply_patch_for_muse_only(self):
        body = {
            "model": "meta/muse-spark-1.3-contributor",
            "tools": [
                {
                    "type": "custom",
                    "name": "apply_patch",
                    "format": {"type": "grammar", "syntax": "lark", "definition": "start: hunk"},
                },
                {
                    "type": "function",
                    "name": "list_threads",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"limit": {"type": "integer"}},
                    },
                },
            ],
        }
        updated, patched = rewrite_request_body(body)
        self.assertGreaterEqual(patched, 1)
        self.assertEqual([tool.get("name") for tool in updated["tools"]], ["list_threads"])

        other = {
            "model": "openai/gpt-5.6-luna",
            "tools": [
                {"type": "custom", "name": "apply_patch", "format": {"type": "grammar"}},
            ],
        }
        unchanged, count = rewrite_request_body(other)
        self.assertEqual(count, 0)
        self.assertEqual(unchanged["tools"][0]["name"], "apply_patch")

    def test_bytes_rewrite_updates_payload(self):
        raw = json.dumps(self._list_threads_request("meta/muse-spark-1.3-contributor")).encode()
        updated, patched, _ = rewrite_request_bytes(raw)
        self.assertEqual(patched, 1)
        self.assertEqual(json.loads(updated)["tools"][0]["parameters"]["required"], ["limit"])


if __name__ == "__main__":
    unittest.main()
