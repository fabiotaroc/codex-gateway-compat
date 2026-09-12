#!/usr/bin/env bash
set -euo pipefail

TOOL="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$TOOL/../.." && pwd)"
RAYCAST_DIR="$HOME/.codex/raycast-scripts"
SWIFTBAR_DIR="$HOME/Library/Application Support/SwiftBar/Plugins"
BIN_DIR="$HOME/.local/bin"

chmod +x "$TOOL/codex-route" "$TOOL/codex_route.py"
chmod +x "$TOOL/raycast/"*.sh

mkdir -p "$BIN_DIR" "$RAYCAST_DIR" "$SWIFTBAR_DIR"

ln -sfn "$TOOL/codex-route" "$BIN_DIR/codex-route"
for name in toggle vercel subscription status; do
  ln -sfn "$TOOL/raycast/${name}.sh" "$RAYCAST_DIR/${name}.sh"
done

# The SwiftBar plugin is generated here rather than kept in the repo:
# SwiftBar follows plugin symlinks and recreates the target as a relative
# Users/... tree, so a real file with an absolute path is required.
rm -f "$SWIFTBAR_DIR/codex-route.30s.sh"
PY="$TOOL/codex_route.py"
cat > "$SWIFTBAR_DIR/codex-route.30s.sh" <<EOF
#!/usr/bin/env bash
# <xbar.title>Codex Route</xbar.title>
# <xbar.desc>Toggle the Codex default between Vercel and subscription</xbar.desc>
if [[ \$# -gt 0 ]]; then
  exec python3 $(printf '%q' "$PY") "\$@"
fi
exec python3 $(printf '%q' "$PY") swiftbar
EOF
chmod +x "$SWIFTBAR_DIR/codex-route.30s.sh"
rm -rf "$SWIFTBAR_DIR/Users"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is not installed; skip SwiftBar app install." >&2
elif [[ -d /Applications/SwiftBar.app ]]; then
  echo "SwiftBar is already installed."
else
  echo "Installing SwiftBar…"
  brew install --cask swiftbar
fi

if [[ -d /Applications/SwiftBar.app ]]; then
  defaults write com.ameba.SwiftBar PluginDirectory -string "$SWIFTBAR_DIR"
  osascript -e 'tell application "SwiftBar" to quit' >/dev/null 2>&1 || true
  sleep 1
  open -g -j -a SwiftBar
fi

if [[ ! -f "$HOME/.codex/config.toml" ]]; then
  echo "No ~/.codex/config.toml yet; skip adopt." >&2
else
  python3 "$TOOL/codex_route.py" adopt
fi

echo
echo "Codex Route is installed."
echo "  CLI:        $BIN_DIR/codex-route status|toggle|vercel|subscription"
echo "  Raycast:    add this Script Directory:"
echo "              $RAYCAST_DIR"
echo "              Raycast → Extensions → Script Commands → Add Script Directory"
echo "  SwiftBar:   $SWIFTBAR_DIR/codex-route.30s.sh"
echo "  Source:     $REPO/tools/codex-route"
echo
if ! echo ":$PATH:" | grep -q ":$BIN_DIR:"; then
  echo "Add $BIN_DIR to PATH to run \`codex-route\` from any terminal."
fi
echo "Switching restarts Codex Desktop so the new default applies; then start a new chat."
