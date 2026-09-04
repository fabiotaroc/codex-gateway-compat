# Codex Gateway Compat

A local HTTP proxy that sits between [Codex](https://openai.com/codex/) and an OpenAI-compatible Responses API gateway (typically [Vercel AI Gateway](https://vercel.com/docs/ai-gateway)). It rewrites requests and streamed responses so non-OpenAI models can use Codex Desktop the same way Codex-native models do: tools, plugins, and token streaming.

Codex Desktop speaks the OpenAI Responses API. Other model providers are stricter about JSON Schema, do not understand namespaced tools, and emit integer arguments as floats. This proxy normalizes that wire format in both directions.

## What it fixes

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

## How it works

```
Codex Desktop
    │  POST /codex/v1/responses
    ▼
this proxy   (default http://127.0.0.1:18787)
    │  rewrite request  →  stream rewritten SSE back
    ▼
AI Gateway   (default https://ai-gateway.vercel.sh)
```

**Request (`rewrite.py`).** For every non-OpenAI model, namespaced tool wrappers become ordinary functions named `<namespace>--<tool>` (for example `mcp__codex_apps__notion--_fetch`). The same flatten is applied to `tool_search_output` items in conversation history, and those discovered tools are copied into `tools[]` so the model can call them. Muse Spark requests also get schema normalization and have unsupported `custom` tools removed.

**Response (`stream.py`).** Flat function names are mapped back to `{ "namespace", "name" }` so Codex can dispatch the real plugin. Integral floats in tool arguments become integers. On SSE streams this happens incrementally: argument deltas that might contain `100.0` split across chunks are replaced with one corrected delta at `.done`.

The mapping is built per request, so other models on the same provider URL are not affected unless they themselves need flattening.

## Setup

Python 3.9+ from the standard library. No third-party packages.

### 1. Run the proxy

```bash
python3 proxy.py --host 127.0.0.1 --port 18787 --upstream https://ai-gateway.vercel.sh
```

Or with environment variables: `CODEX_GATEWAY_COMPAT_HOST`, `CODEX_GATEWAY_COMPAT_PORT`, `CODEX_GATEWAY_COMPAT_UPSTREAM`.

Confirm it is up:

```bash
curl -s http://127.0.0.1:18787/health
```

### 2. Point Codex at it

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

### 3. Keep it running (macOS)

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
    <string>/PATH/TO/codex-gateway-compat/proxy.py</string>
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

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.codex-gateway-compat.plist
launchctl kickstart -k gui/$(id -u)/com.example.codex-gateway-compat
```

## Usage

1. Start the proxy (or let launchd start it).
2. In Codex Desktop, pick a non-OpenAI model on the proxied provider.
3. Chat as usual. Tool calls and plugin tools should dispatch; tokens should stream.

To skip rewriting, switch that model to the direct provider.

## Development

```
codex-gateway-compat/
  proxy.py          HTTP server and streaming relay
  rewrite.py        Request-side schema and namespace rewrites
  stream.py         Response-side un-flattening and float coercion
  tests/            Unit and local integration tests
  var/              Runtime logs and last-request dumps (gitignored)
```

```bash
python3 -m unittest discover -s tests -v
```

Successful rewrites can write `var/last-request.json`, `var/last-tools.json`, and `var/last-upstream-error.json` for debugging. Those files are not committed.

## Limits

- `tool_search` is an internal Codex tool and may not appear as a visible tool call in the Desktop UI. After flattening, models should call the plugin tool directly instead of looping on search.
- Muse Spark does not receive `apply_patch`. Use the shell tool, or an OpenAI model, if you need the grammar patch format.
- Codex Desktop updates can change the Responses wire format. This is a local shim, not a supported Codex feature. If a new release 400s, inspect `var/last-upstream-error.json` and the session rollout under `~/.codex/sessions/`.
