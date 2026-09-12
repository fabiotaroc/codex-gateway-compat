#!/usr/bin/env python3
"""Switch the Codex Desktop/CLI default between Vercel gateway and subscription."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, NoReturn, Optional


BEGIN_MARK = "# BEGIN CODEX-ROUTE"
END_MARK = "# END CODEX-ROUTE"
# Codex Desktop ships inside ChatGPT.app and only reads config.toml at startup.
CODEX_APP_BUNDLE = os.environ.get("CODEX_ROUTE_APP_BUNDLE", "com.openai.codex")
CODEX_APP_QUIT_TIMEOUT_S = 20.0
ROUTE_NAMES = ("vercel", "subscription")
MANAGED_KEYS = ("model", "model_provider", "model_reasoning_effort")
LEGACY_COMMENT_PREFIXES = (
    "# Comment/uncomment this block to switch subs",
    "# Only to configure default at start",
)
ASSIGNMENT_RE = re.compile(
    r"^\s*#?\s*(model|model_provider|model_reasoning_effort)\s*="
)
ROUTE_LINE_RE = re.compile(r"^# Route:\s*(\S+)\s*$")
KEY_LINE_RE = re.compile(
    r"^\s*(model|model_provider|model_reasoning_effort)\s*=\s*(.+?)\s*$"
)
BLOCK_RE = re.compile(
    re.escape(BEGIN_MARK) + r".*?" + re.escape(END_MARK) + r"\n?",
    re.DOTALL,
)


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    model: str
    model_provider: str
    model_reasoning_effort: Optional[str] = None


@dataclass(frozen=True)
class RouteState:
    route: Optional[str]
    keys: Mapping[str, str]
    managed: bool
    # Managed keys that also appear outside the block (e.g. appended by Codex
    # Desktop). TOML rejects duplicate keys, so these break Codex at startup.
    conflicts: tuple[str, ...] = ()


def default_config_path() -> Path:
    override = os.environ.get("CODEX_ROUTE_CONFIG")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("CODEX_HOME")
    if home:
        return Path(home).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def default_presets_path() -> Path:
    override = os.environ.get("CODEX_ROUTE_PRESETS")
    if override:
        return Path(override).expanduser()
    home_override = Path.home() / ".codex" / "codex-route.presets.json"
    if home_override.is_file():
        return home_override
    return Path(__file__).with_name("presets.json")


def load_presets(path: Optional[Path] = None) -> dict[str, Preset]:
    presets_path = path or default_presets_path()
    raw = json.loads(presets_path.read_text(encoding="utf-8"))
    presets: dict[str, Preset] = {}
    for name in ROUTE_NAMES:
        if name not in raw:
            raise SystemExit(f"presets file missing {name!r}: {presets_path}")
        item = raw[name]
        presets[name] = Preset(
            name=name,
            label=str(item.get("label") or name),
            model=str(item["model"]),
            model_provider=str(item["model_provider"]),
            model_reasoning_effort=item.get("model_reasoning_effort") or None,
        )
    return presets


def parse_toml_scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def split_preamble(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("["):
            return "".join(lines[:index]), "".join(lines[index:])
    return text, ""


def read_assignments(text: str) -> dict[str, str]:
    keys: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("["):
            break
        match = KEY_LINE_RE.match(line)
        if match:
            keys[match.group(1)] = parse_toml_scalar(match.group(2))
    return keys


def match_route(keys: Mapping[str, str], presets: Mapping[str, Preset]) -> Optional[str]:
    for name, preset in presets.items():
        if keys.get("model") == preset.model and keys.get("model_provider") == preset.model_provider:
            return name
    provider = keys.get("model_provider")
    if provider == "vercel":
        return "vercel"
    if provider == "openai":
        return "subscription"
    if not keys:
        return "subscription"
    return None


def stray_managed_keys(preamble_without_block: str) -> tuple[str, ...]:
    found: list[str] = []
    for line in preamble_without_block.splitlines():
        match = KEY_LINE_RE.match(line)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return tuple(found)


def inspect_text(text: str, presets: Mapping[str, Preset]) -> RouteState:
    block = BLOCK_RE.search(text)
    if block:
        body = block.group(0)
        route = None
        for line in body.splitlines():
            route_match = ROUTE_LINE_RE.match(line)
            if route_match and route_match.group(1) in presets:
                route = route_match.group(1)
                break
        keys = read_assignments(body)
        preamble, _ = split_preamble(BLOCK_RE.sub("", text, count=1))
        return RouteState(
            route=route or match_route(keys, presets),
            keys=keys,
            managed=True,
            conflicts=stray_managed_keys(preamble),
        )
    return RouteState(route=match_route(read_assignments(text), presets), keys=read_assignments(text), managed=False)


def render_block(preset: Preset) -> str:
    lines = [
        BEGIN_MARK,
        "# Managed by tools/codex-route. Do not edit this block by hand.",
        f"# Route: {preset.name}",
        f'model = "{preset.model}"',
        f'model_provider = "{preset.model_provider}"',
    ]
    if preset.model_reasoning_effort:
        lines.append(f'model_reasoning_effort = "{preset.model_reasoning_effort}"')
    lines.append(END_MARK)
    return "\n".join(lines) + "\n"


def _is_legacy_comment(line: str) -> bool:
    stripped = line.strip()
    return any(stripped.startswith(prefix) for prefix in LEGACY_COMMENT_PREFIXES)


def strip_legacy_preamble(preamble: str) -> str:
    kept: list[str] = []
    for line in preamble.splitlines(keepends=True):
        if ASSIGNMENT_RE.match(line) or _is_legacy_comment(line):
            continue
        if BLOCK_RE.fullmatch(line.strip() + "\n"):
            continue
        kept.append(line)
    while kept and kept[0].strip() == "":
        kept.pop(0)
    return "".join(kept)


def duplicate_top_level_keys(text: str) -> list[str]:
    preamble, _ = split_preamble(text)
    seen: dict[str, int] = {}
    for line in preamble.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_.\"'-]+)\s*=", line)
        if match:
            seen[match.group(1)] = seen.get(match.group(1), 0) + 1
    return sorted(key for key, count in seen.items() if count > 1)


def apply_route(text: str, preset: Preset) -> str:
    # Always rebuild: drop any existing block, then remove every top-level
    # copy of the managed keys (legacy comments, or lines Codex Desktop
    # appended itself), so the fresh block is the single source of truth.
    without_block = BLOCK_RE.sub("", text, count=1)
    preamble, rest = split_preamble(without_block)
    cleaned = strip_legacy_preamble(preamble)
    if cleaned and not cleaned.startswith("\n"):
        cleaned = "\n" + cleaned
    updated = render_block(preset) + cleaned + rest
    duplicates = duplicate_top_level_keys(updated)
    if duplicates:
        raise RuntimeError(
            "refusing to write config.toml with duplicate top-level keys: "
            + ", ".join(duplicates)
        )
    return updated


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".codex-route-", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            if content and not content.endswith("\n"):
                handle.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def other_route(route: str) -> str:
    if route == "vercel":
        return "subscription"
    if route == "subscription":
        return "vercel"
    unreachable: NoReturn
    unreachable = route  # type: ignore[assignment]
    raise AssertionError(f"unhandled route: {unreachable}")


def format_status(state: RouteState, presets: Mapping[str, Preset]) -> str:
    if state.route and state.route in presets:
        preset = presets[state.route]
        text = f"{preset.label}: {preset.model} via {preset.model_provider}"
        if not state.managed:
            text += " (unmanaged)"
    else:
        model = state.keys.get("model", "(default)")
        provider = state.keys.get("model_provider", "(default)")
        text = f"Unknown: {model} via {provider}"
    if state.conflicts:
        text += (
            f" — CONFLICT: duplicate {', '.join(state.conflicts)} outside the block;"
            " Codex will not start. Run `codex-route repair`."
        )
    return text


def notify(title: str, message: str) -> None:
    if os.environ.get("CODEX_ROUTE_NO_NOTIFY"):
        return
    script = (
        f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    )
    subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def refresh_swiftbar() -> None:
    if os.environ.get("CODEX_ROUTE_NO_NOTIFY"):
        return
    subprocess.run(
        ["open", "-g", "swiftbar://refreshallplugins"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _quiet_run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def codex_app_running() -> bool:
    result = _quiet_run(
        ["osascript", "-e", f'application id "{CODEX_APP_BUNDLE}" is running']
    )
    return result.stdout.strip() == "true"


def restart_codex_app() -> bool:
    """Quit Codex Desktop gracefully and relaunch it. Returns True if restarted."""
    if not codex_app_running():
        return False
    _quiet_run(["osascript", "-e", f'tell application id "{CODEX_APP_BUNDLE}" to quit'])
    deadline = time.monotonic() + CODEX_APP_QUIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if not codex_app_running():
            break
        time.sleep(0.25)
    else:
        # The app ignored the quit request (or a dialog is blocking it).
        # Leave it alone rather than killing it mid-task.
        notify("Codex Route", "Codex did not quit; restart it to apply the new default.")
        return False
    time.sleep(0.5)
    _quiet_run(["open", "-b", CODEX_APP_BUNDLE])
    return True


def write_route(
    path: Path, text: str, preset: Preset, *, announce: bool, restart: bool = False
) -> RouteState:
    atomic_write(path, apply_route(text, preset))
    state = RouteState(
        route=preset.name,
        keys={
            "model": preset.model,
            "model_provider": preset.model_provider,
            **(
                {"model_reasoning_effort": preset.model_reasoning_effort}
                if preset.model_reasoning_effort
                else {}
            ),
        },
        managed=True,
    )
    if announce:
        suffix = " · restarting Codex" if restart and codex_app_running() else ""
        notify("Codex Route", f"Default → {preset.label} ({preset.model}){suffix}")
        refresh_swiftbar()
    if restart:
        restart_codex_app()
    return state


def resolve_target(action: str, state: RouteState) -> str:
    if action in ROUTE_NAMES:
        return action
    if action == "toggle":
        if state.route in ROUTE_NAMES:
            return other_route(state.route)
        raise SystemExit(
            "current Codex default is not a known route; pass vercel or subscription"
        )
    if action in ("adopt", "repair"):
        if state.route in ROUTE_NAMES:
            return state.route
        raise SystemExit(
            f"cannot {action} an unknown Codex default; pass vercel or subscription first"
        )
    raise SystemExit(f"unhandled action: {action}")


def swiftbar_cli(script_path: Path) -> Path:
    # Use the repo wrapper, not SWIFTBAR_PLUGIN_PATH: the plugin folder lives
    # under "Application Support", and SwiftBar splits parameters on spaces.
    return script_path.resolve().with_name("codex-route")


def swiftbar_item(title: str, cli: Path, action: str, *, checked: bool = False) -> str:
    # SwiftBar only enables a row when it recognizes an action.
    extras = " checked=true" if checked else ""
    return f'{title} | bash="{cli}" param1={action} terminal=false refresh=true{extras}'


def swiftbar_output(state: RouteState, presets: Mapping[str, Preset], script_path: Path) -> str:
    cli = swiftbar_cli(script_path)
    label = presets[state.route].label if state.route in presets else "Codex"
    title = "Sub" if state.route == "subscription" else label
    if state.conflicts:
        title = f"⚠︎ {title}"
    lines = [title, "---"]
    if state.conflicts:
        lines.append(
            swiftbar_item(
                f"Repair config (duplicate {', '.join(state.conflicts)})", cli, "repair"
            )
        )
        lines.append("---")
    for name in ROUTE_NAMES:
        preset = presets[name]
        lines.append(swiftbar_item(preset.label, cli, name, checked=state.route == name))
    lines.append("---")
    lines.append("Switching restarts Codex so new chats use the default. | refresh=true")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "status", "toggle", "vercel", "subscription", "sub", "gateway",
            "adopt", "repair", "swiftbar",
        ),
        help="status, adopt/repair current values, or switch the default route",
    )
    parser.add_argument("--config", type=Path, help="config.toml path")
    parser.add_argument("--presets", type=Path, help="presets JSON path")
    parser.add_argument("--no-notify", action="store_true", help="skip macOS notification")
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="do not quit and relaunch Codex Desktop after switching",
    )
    parser.add_argument("--json", action="store_true", help="print status as JSON")
    return parser


def normalize_action(action: str) -> str:
    if action == "sub":
        return "subscription"
    if action == "gateway":
        return "vercel"
    return action


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_notify:
        os.environ["CODEX_ROUTE_NO_NOTIFY"] = "1"

    presets = load_presets(args.presets)
    config_path = args.config or default_config_path()
    if not config_path.is_file():
        raise SystemExit(f"Codex config not found: {config_path}")

    text = config_path.read_text(encoding="utf-8")
    state = inspect_text(text, presets)
    action = normalize_action(args.action)

    if action == "status":
        if args.json:
            print(
                json.dumps(
                    {
                        "route": state.route,
                        "managed": state.managed,
                        "keys": dict(state.keys),
                        "conflicts": list(state.conflicts),
                        "config": str(config_path),
                    },
                    indent=2,
                )
            )
        else:
            print(format_status(state, presets))
        return 0

    if action == "swiftbar":
        sys.stdout.write(swiftbar_output(state, presets, Path(__file__).resolve()))
        return 0

    target = resolve_target(action, state)
    preset = presets[target]
    if action == "adopt" and state.managed and state.route == target and not state.conflicts:
        print(format_status(state, presets))
        return 0
    if action == "repair" and not state.conflicts and state.managed:
        print(format_status(state, presets))
        return 0

    # adopt only normalizes the file; switching and repair also restart Codex
    # (repair exists because a duplicate key stops Codex from starting).
    restart_wanted = action != "adopt"
    restart = restart_wanted and not args.no_restart and not os.environ.get("CODEX_ROUTE_NO_RESTART")
    new_state = write_route(config_path, text, preset, announce=action != "adopt", restart=restart)
    print(format_status(new_state, presets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
