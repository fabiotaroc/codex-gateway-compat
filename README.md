# Codex Gateway Compat

Run [Codex](https://openai.com/codex/) Desktop on non-OpenAI models through an OpenAI-compatible gateway (typically [Vercel AI Gateway](https://vercel.com/docs/ai-gateway)), and switch between that gateway and your ChatGPT subscription with one click.

Two pieces, both local, both standard-library Python:

| Piece | What it does | Where |
|---|---|---|
| **Proxy** | Sits between Codex and the gateway. Rewrites requests and streamed responses so non-OpenAI models get tools, plugins, and token streaming the same way Codex-native models do. | `proxy/` |
| **Route toggle** | Flips the Codex *default* model between the gateway and the subscription, then restarts Codex Desktop so it takes effect. Exposed as a CLI, Raycast commands, and a macOS menu-bar item. | `tools/codex-route/` |

The proxy is what makes the gateway usable. The toggle is what makes moving back and forth painless.

## Part 1: The proxy

Codex Desktop speaks the OpenAI Responses API. Other model providers are stricter about JSON Schema, do not understand namespaced tools, and emit integer arguments as floats. The proxy normalizes that wire format in both directions.

### What it fixes

| Problem | Who hits it | What the proxy does |
|---|---|---|
| Strict tool JSON Schema (`required` must list every property, no root `oneOf` without `type: object`) | Muse Spark | Completes and tightens schemas |
| `custom` / grammar tools (`apply_patch`) | Muse Spark | Drops them from the request |
| `type: "namespace"` tool groups (`mcp__codex_apps__notion` wrapping `_fetch`) | All non-OpenAI models | Flattens to `namespace--tool` functions |
| Plugin tools only appear inside `tool_search` results | All non-OpenAI models | Promotes them into top-level `tools[]` |
| `max_output_tokens: 100.0` rejected by Codex | All non-OpenAI models | Coerces integral floats to ints on the way back |
| Duplicate `Content-Length` after a rewrite | Any rewritten request | Sends a single correct length |
| SSE buffered until the full response arrives | Any streaming request | Relays chunks as they arrive |

OpenAI models (`openai/…`, `gpt-…`, `o1` / `o3` / `o4`, `codex`) are forwarded unchanged.

### How it works

```
Codex Desktop
    │  POST /codex/v1/responses
    ▼
this proxy   (default http://127.0.0.1:18787)
    │  rewrite request  →  stream rewritten SSE back
    ▼
AI Gateway   (default https://ai-gateway.vercel.sh)
```

**Request (`proxy/rewrite.py`).** For every non-OpenAI model, namespaced tool wrappers become ordinary functions named `<namespace>--<tool>` (for example `mcp__codex_apps__notion--_fetch`). The same flatten is applied to `tool_search_output` items in conversation history, and those discovered tools are copied into `tools[]` so the model can call them. Muse Spark requests also get schema normalization and have unsupported `custom` tools removed.

**Response (`proxy/stream.py`).** Flat function names are mapped back to `{ "namespace", "name" }` so Codex can dispatch the real plugin. Integral floats in tool arguments become integers. On SSE streams this happens incrementally: argument deltas that might contain `100.0` split across chunks are replaced with one corrected delta at `.done`.

The mapping is built per request, so other models on the same provider URL are not affected unless they themselves need flattening.

### Setup

Python 3.9+ from the standard library. No third-party packages.

#### 1. Run the proxy

From the repo root:

```bash
python3 -m proxy.server --host 127.0.0.1 --port 18787 --upstream https://ai-gateway.vercel.sh
```

Or with environment variables: `CODEX_GATEWAY_COMPAT_HOST`, `CODEX_GATEWAY_COMPAT_PORT`, `CODEX_GATEWAY_COMPAT_UPSTREAM`.

Confirm it is up:

```bash
curl -s http://127.0.0.1:18787/health
```

#### 2. Point Codex at it

In `~/.codex/config.toml`, send the gateway provider through the proxy and keep a direct provider for bypass:

```toml
[model_providers.vercel]
name = "Vercel AI Gateway"
base_url = "http://127.0.0.1:18787/codex/v1"
env_key = "AI_GATEWAY_API_KEY"
wire_api = "responses"

[model_providers.vercel_direct]
name = "Vercel AI Gateway (direct)"
base_url = "https://ai-gateway.vercel.sh/codex/v1"
env_key = "AI_GATEWAY_API_KEY"
wire_api = "responses"
```

Any model whose `model_provider` is `vercel` goes through the proxy, including models chosen in the Codex Desktop picker. Use `vercel_direct` when you want the raw gateway.

The proxy forwards the `Authorization` header Codex already sends. It does not store API keys.

#### 3. Keep it running (macOS)

A launch agent starts the proxy at login. Example plist (`~/Library/LaunchAgents/com.example.codex-gateway-compat.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.codex-gateway-compat</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>-m</string>
    <string>proxy.server</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>18787</string>
    <string>--upstream</string>
    <string>https://ai-gateway.vercel.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/PATH/TO/codex-gateway-compat</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/PATH/TO/codex-gateway-compat/var/proxy.log</string>
  <key>StandardErrorPath</key>
  <string>/PATH/TO/codex-gateway-compat/var/proxy.err.log</string>
</dict>
</plist>
```

`WorkingDirectory` must be the repo root so `python3 -m proxy.server` can find the package.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.codex-gateway-compat.plist
launchctl kickstart -k gui/$(id -u)/com.example.codex-gateway-compat
```

### Using the proxy

1. Start the proxy (or let launchd start it).
2. In Codex Desktop, pick a non-OpenAI model on the proxied provider.
3. Chat as usual. Tool calls and plugin tools should dispatch; tokens should stream.

To skip rewriting, switch that model to the direct provider.

## Part 2: The route toggle

The provider table above tells Codex *how* to reach the gateway. Which one Codex uses by default is decided by three top-level keys in the same file: `model`, `model_provider`, and `model_reasoning_effort`. Flipping them by hand means editing `config.toml`, quitting Codex Desktop, and reopening it. Desktop does reread the config while running, but it never revalidates the model already selected in the composer, so without a restart the picker can keep a slug the new provider rejects.

`codex-route` does all of that in one step. It keeps those three keys in a marked block at the top of `~/.codex/config.toml`, rewrites the block atomically, and leaves the rest of the file untouched:

```toml
# BEGIN CODEX-ROUTE
# Managed by tools/codex-route. Do not edit this block by hand.
# Route: vercel
model = "meta/muse-spark-1.3-contributor"
model_provider = "vercel"
model_reasoning_effort = "xhigh"
# END CODEX-ROUTE
```

The two routes are defined in `tools/codex-route/presets.json` (override per machine with `~/.codex/codex-route.presets.json`):

| Route | Model | Provider | Goes through the proxy? |
|---|---|---|---|
| `vercel` | `meta/muse-spark-1.3-contributor` | `vercel` | Yes |
| `subscription` | `gpt-5.6-sol` | `openai` | No, uses your ChatGPT login |

### Install

```bash
tools/codex-route/install.sh
```

This links a `codex-route` command into `~/.local/bin`, links four Raycast Script Commands into `~/.codex/raycast-scripts`, generates a SwiftBar plugin (installing SwiftBar via Homebrew if needed), and adopts whatever default `config.toml` currently has. Then add `~/.codex/raycast-scripts` under Raycast → Extensions → Script Commands → Script Folders.

### Three ways to switch

**Terminal**

```bash
codex-route status
codex-route vercel
codex-route subscription
codex-route toggle
```

**Raycast**: search for `Codex Route: Vercel`, `Codex Route: Subscription`, `Codex Route: Toggle`, or `Codex Route: Status`.

**Menu bar (SwiftBar)**: an item reading `Vercel` or `Sub` next to the clock. Click it and pick the other route.

Every switch writes the block, posts a macOS notification, asks Codex Desktop (`ChatGPT.app`) to quit gracefully, waits for it, and relaunches it. If Codex is not running it stays closed. If Codex refuses to quit within 20 seconds (a dialog is open, or a turn is mid-flight), the file is still updated and nothing is force-killed — but the switch has *not* taken effect, so `codex-route` exits `1`, warns on stderr, and records a pending-restart flag. `codex-route status` reports it, and the menu-bar item shows `⚠︎` with a **Restart Codex to apply …** entry; `codex-route restart` retries the quit and relaunch without rewriting the file. Use `--no-restart` to edit the file only.

After the restart, **start a new chat**. Existing threads keep the provider they were created with.

Full details, including how the SwiftBar plugin is generated, are in [`tools/codex-route/README.md`](tools/codex-route/README.md).

## Development

```
codex-gateway-compat/
  proxy/
    server.py           HTTP server and streaming relay (python3 -m proxy.server)
    rewrite.py          Request-side schema and namespace rewrites
    stream.py           Response-side un-flattening and float coercion
  tools/codex-route/
    codex_route.py      Route toggle: config block, restart, SwiftBar menu
    codex-route         CLI wrapper
    presets.json        The two routes
    raycast/            Raycast Script Commands
    install.sh          Links CLI + Raycast, generates the SwiftBar plugin
  tests/                Unit and local integration tests for both pieces
  var/                  Runtime logs and last-request dumps (gitignored)
```

```bash
python3 -m unittest discover -s tests -v
```

Successful rewrites can write `var/last-request.json`, `var/last-tools.json`, and `var/last-upstream-error.json` for debugging. Those files are not committed.

## Limits

- `tool_search` is an internal Codex tool and may not appear as a visible tool call in the Desktop UI. After flattening, models should call the plugin tool directly instead of looping on search.
- Muse Spark does not receive `apply_patch`. Use the shell tool, or an OpenAI model, if you need the grammar patch format.
- Codex Desktop updates can change the Responses wire format. This is a local shim, not a supported Codex feature. If a new release 400s, inspect `var/last-upstream-error.json` and the session rollout under `~/.codex/sessions/`.
- The route toggle changes only the *default* for new chats. The Desktop model picker can still choose any configured model per conversation, and old threads keep their original provider.
- Codex Desktop persists some of the same keys itself (notably `model_reasoning_effort`) by appending them below the managed block, which makes the file invalid TOML (`duplicate key`) until the toggle runs again. `codex-route status` and the menu-bar item flag this; `codex-route repair` fixes it and restarts Codex.
- The toggle relies on Codex Desktop honoring a normal quit request. It will not kill the app; if a task is mid-flight and Codex asks for confirmation, finish or cancel that first, then run `codex-route restart`. Until you do, Desktop keeps the model that was selected before the switch, and submitting on it fails with an error that blames your account (`not supported with your ChatGPT account`) rather than the route. `codex-route status` and the menu-bar item report this as pending.
