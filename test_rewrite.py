#!/usr/bin/env python3
import json
import unittest

from rewrite import complete_required, is_muse_model, rewrite_request_body, rewrite_request_bytes


class MuseModelTests(unittest.TestCase):
    def test_matches_contributor_and_paid(self):
        self.assertTrue(is_muse_model("meta/muse-spark-1.3-contributor"))
        self.assertTrue(is_muse_model("meta/muse-spark-1.3"))
        self.assertFalse(is_muse_model("openai/gpt-5.5"))
        self.assertFalse(is_muse_model("deepseek-v4-flash"))


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

    def test_rewrites_namespaced_tools(self):
        body = {
            "model": "meta/muse-spark-1.3",
            "tools": [
                {
                    "type": "namespace",
                    "name": "codex_app",
                    "tools": [
                        {
                            "name": "list_archived_threads",
                            "parameters": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"limit": {"type": "integer"}},
                            },
                        }
                    ],
                }
            ],
        }
        updated, patched = rewrite_request_body(body)
        self.assertEqual(patched, 1)
        self.assertEqual(updated["tools"][0]["tools"][0]["parameters"]["required"], ["limit"])

    def test_bytes_round_trip_leaves_non_json_alone(self):
        raw, patched = rewrite_request_bytes(b"not-json")
        self.assertEqual(raw, b"not-json")
        self.assertEqual(patched, 0)

    def test_bytes_rewrite_updates_payload(self):
        raw = json.dumps(self._list_threads_request("meta/muse-spark-1.3-contributor")).encode()
        updated, patched = rewrite_request_bytes(raw)
        self.assertEqual(patched, 1)
        self.assertEqual(json.loads(updated)["tools"][0]["parameters"]["required"], ["limit"])


if __name__ == "__main__":
    unittest.main()
