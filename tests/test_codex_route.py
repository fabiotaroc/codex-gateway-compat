#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "codex-route"))

from codex_route import (  # noqa: E402
    apply_route,
    duplicate_top_level_keys,
    format_status,
    inspect_text,
    load_presets,
    main,
    other_route,
    swiftbar_cli,
    swiftbar_output,
)


LEGACY_VERCEL = """# Comment/uncomment this block to switch subs
model = "meta/muse-spark-1.3-contributor"
model_provider = "vercel"
model_reasoning_effort = "xhigh"

# Only to configure default at start. Leaving commented out uses subscription
# model = "gpt-5.6-sol"
# model_provider = "openai"

personality = "pragmatic"

[model_providers.vercel]
name = "Vercel AI Gateway"
base_url = "http://127.0.0.1:18787/codex/v1"
"""

LEGACY_SUBSCRIPTION = """# Comment/uncomment this block to switch subs
# model = "meta/muse-spark-1.3-contributor"
# model_provider = "vercel"
# model_reasoning_effort = "xhigh"

# Only to configure default at start. Leaving commented out uses subscription
model = "gpt-5.6-sol"
model_provider = "openai"

personality = "pragmatic"

[model_providers.vercel]
name = "Vercel AI Gateway"
"""

COMMENTED_DEFAULT = """# Comment/uncomment this block to switch subs
# model = "meta/muse-spark-1.3-contributor"
# model_provider = "vercel"
# model_reasoning_effort = "xhigh"

personality = "pragmatic"
"""


class CodexRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.presets = load_presets()

    def test_detects_legacy_vercel_header(self):
        state = inspect_text(LEGACY_VERCEL, self.presets)
        self.assertEqual(state.route, "vercel")
        self.assertFalse(state.managed)
        self.assertEqual(state.keys["model"], "meta/muse-spark-1.3-contributor")

    def test_detects_legacy_subscription_header(self):
        state = inspect_text(LEGACY_SUBSCRIPTION, self.presets)
        self.assertEqual(state.route, "subscription")
        self.assertFalse(state.managed)

    def test_commented_header_counts_as_subscription(self):
        state = inspect_text(COMMENTED_DEFAULT, self.presets)
        self.assertEqual(state.route, "subscription")

    def test_toggle_writes_managed_subscription_block(self):
        updated = apply_route(LEGACY_VERCEL, self.presets["subscription"])
        state = inspect_text(updated, self.presets)
        self.assertEqual(state.route, "subscription")
        self.assertTrue(state.managed)
        self.assertIn('model = "gpt-5.6-sol"', updated)
        self.assertIn('model_provider = "openai"', updated)
        self.assertNotIn("model_reasoning_effort", updated)
        self.assertNotIn("# Comment/uncomment this block to switch subs", updated)
        self.assertNotIn("# model = \"gpt-5.6-sol\"", updated)
        self.assertIn('personality = "pragmatic"', updated)
        self.assertIn("[model_providers.vercel]", updated)
        self.assertEqual(updated.count("model_provider ="), 1)

    def test_toggle_round_trip_preserves_rest_of_file(self):
        subscription = apply_route(LEGACY_VERCEL, self.presets["subscription"])
        vercel = apply_route(subscription, self.presets["vercel"])
        self.assertEqual(inspect_text(vercel, self.presets).route, "vercel")
        self.assertIn("model_reasoning_effort = \"xhigh\"", vercel)
        self.assertIn('personality = "pragmatic"', vercel)
        self.assertIn("[model_providers.vercel]", vercel)
        self.assertIn("http://127.0.0.1:18787/codex/v1", vercel)

    def test_second_write_replaces_block_once(self):
        first = apply_route(LEGACY_VERCEL, self.presets["subscription"])
        second = apply_route(first, self.presets["vercel"])
        self.assertEqual(second.count("# BEGIN CODEX-ROUTE"), 1)
        self.assertEqual(second.count("# END CODEX-ROUTE"), 1)

    def test_detects_and_repairs_key_appended_by_codex_desktop(self):
        managed = apply_route(LEGACY_VERCEL, self.presets["vercel"])
        # Codex Desktop appends persisted settings at the end of the preamble.
        broken = managed.replace(
            'personality = "pragmatic"\n',
            'personality = "pragmatic"\nmodel_reasoning_effort = "low"\n',
        )
        state = inspect_text(broken, self.presets)
        self.assertEqual(state.route, "vercel")
        self.assertEqual(state.conflicts, ("model_reasoning_effort",))
        self.assertIn("CONFLICT", format_status(state, self.presets))
        self.assertIn("⚠︎ Vercel", swiftbar_output(state, self.presets, Path(__file__)))
        self.assertIn("param1=repair", swiftbar_output(state, self.presets, Path(__file__)))

        repaired = apply_route(broken, self.presets["vercel"])
        self.assertEqual(repaired.count("model_reasoning_effort ="), 1)
        self.assertIn('model_reasoning_effort = "xhigh"', repaired)
        self.assertEqual(inspect_text(repaired, self.presets).conflicts, ())
        self.assertIn('personality = "pragmatic"', repaired)
        self.assertEqual(duplicate_top_level_keys(repaired), [])

    def test_refuses_to_write_other_duplicate_keys(self):
        broken = LEGACY_VERCEL.replace(
            'personality = "pragmatic"\n',
            'personality = "pragmatic"\npersonality = "friendly"\n',
        )
        with self.assertRaises(RuntimeError):
            apply_route(broken, self.presets["subscription"])

    def test_other_route_is_exhaustive(self):
        self.assertEqual(other_route("vercel"), "subscription")
        self.assertEqual(other_route("subscription"), "vercel")

    def test_swiftbar_menu_uses_wrapper_actions(self):
        script = Path(__file__).resolve().parent.parent / "tools" / "codex-route" / "codex_route.py"
        state = inspect_text(apply_route(LEGACY_VERCEL, self.presets["subscription"]), self.presets)
        menu = swiftbar_output(state, self.presets, script)
        cli = swiftbar_cli(script)
        self.assertTrue(cli.is_file())
        self.assertIn("Sub\n---\n", menu)
        self.assertNotIn(" ", str(cli))
        self.assertIn(f'Vercel | bash="{cli}" param1=vercel terminal=false refresh=true', menu)
        self.assertIn(
            f'Subscription | bash="{cli}" param1=subscription terminal=false refresh=true checked=true',
            menu,
        )
        self.assertNotIn("Toggle", menu)
        self.assertNotIn("disabled=true", menu)

    def test_switch_restarts_app_but_adopt_does_not(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "codex_route.restart_codex_app"
        ) as restart, mock.patch("codex_route.codex_app_running", return_value=True):
            config = Path(tmp) / "config.toml"
            config.write_text(LEGACY_VERCEL, encoding="utf-8")
            self.assertEqual(main(["--config", str(config), "--no-notify", "adopt"]), 0)
            restart.assert_not_called()
            self.assertEqual(main(["--config", str(config), "--no-notify", "subscription"]), 0)
            restart.assert_called_once()
            restart.reset_mock()
            self.assertEqual(
                main(["--config", str(config), "--no-notify", "--no-restart", "vercel"]), 0
            )
            restart.assert_not_called()

    def test_cli_toggle_and_status_on_temp_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(LEGACY_VERCEL, encoding="utf-8")
            before = json.loads(_capture_stdout(config, "status"))
            self.assertEqual(before["route"], "vercel")
            self.assertFalse(before["managed"])
            self.assertEqual(
                main(["--config", str(config), "--no-notify", "--no-restart", "toggle"]), 0
            )
            payload = json.loads(_capture_stdout(config, "status"))
            self.assertEqual(payload["route"], "subscription")
            self.assertTrue(payload["managed"])
            self.assertEqual(main(["--config", str(config), "--no-notify", "adopt"]), 0)
            again = config.read_text(encoding="utf-8")
            self.assertEqual(again.count("# BEGIN CODEX-ROUTE"), 1)
            self.assertIn('personality = "pragmatic"', again)


def _capture_stdout(config: Path, action: str) -> str:
    buffer = StringIO()
    with mock.patch("sys.stdout", buffer):
        main(["--config", str(config), "--no-notify", "--json", action])
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
