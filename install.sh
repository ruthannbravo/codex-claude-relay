#!/bin/sh
# Installs the Codex <-> Claude relay: one file in ~/.relay and a `relay` command that works in any project.
#
#   curl -fsSL https://raw.githubusercontent.com/ruthannbravo/codex-claude-relay/main/install.sh | sh
#
# Run it again to update. Remove everything it added with: relay uninstall
# Options (environment variables):
#   RELAY_TEACH=yes|no   add a short note about the relay to Claude's and Codex's general instructions (asked if unset)
#   RELAY_SOURCE=...     where to get relay.py (a URL or a local file); defaults to the latest version on GitHub
#   RELAY_HOME=...       where to keep it; defaults to ~/.relay
set -eu

SOURCE="${RELAY_SOURCE:-https://raw.githubusercontent.com/ruthannbravo/codex-claude-relay/main/relay.py}"
HOME_DIR="${RELAY_HOME:-$HOME/.relay}"
MARK='# added by codex-claude-relay'
say() { printf '%s\n' "$*"; }

command -v python3 >/dev/null 2>&1 || { say "The relay needs Python 3. On a Mac, run: xcode-select --install"; exit 1; }
command -v git >/dev/null 2>&1 || { say "The relay needs Git. On a Mac, run: xcode-select --install"; exit 1; }

# 1. Get relay.py, and only replace the old copy if the new one is a valid Python file.
mkdir -p "$HOME_DIR"
case "$SOURCE" in
  http://*|https://*) curl -fsSL "$SOURCE" -o "$HOME_DIR/relay.py.new" || { say "Couldn't download the relay; nothing was changed."; exit 1; } ;;
  *) cp "$SOURCE" "$HOME_DIR/relay.py.new" ;;
esac
if ! python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$HOME_DIR/relay.py.new" 2>/dev/null; then
  rm -f "$HOME_DIR/relay.py.new"; say "The download doesn't look right; nothing was changed."; exit 1
fi
mv "$HOME_DIR/relay.py.new" "$HOME_DIR/relay.py"

# 2. Put a `relay` command in a folder the Terminal already looks in, or add ~/.local/bin to the Terminal's list.
BIN=""
for dir in "$HOME/.local/bin" "$HOME/bin"; do
  case ":$PATH:" in *":$dir:"*) if [ -d "$dir" ] && [ -w "$dir" ]; then BIN="$dir"; break; fi ;; esac
done
PROFILES=""
if [ -z "$BIN" ]; then
  BIN="$HOME/.local/bin"
  mkdir -p "$BIN"
  case "${SHELL:-}" in
    */zsh) FILES="$HOME/.zshrc $HOME/.zprofile" ;;
    */bash) FILES="$HOME/.bashrc $HOME/.bash_profile" ;;
    *) FILES="$HOME/.profile" ;;
  esac
  for file in $FILES; do
    grep -qs "$MARK" "$file" || printf '\nexport PATH="$HOME/.local/bin:$PATH"  %s\n' "$MARK" >> "$file"
    PROFILES="$PROFILES \"$file\","
  done
fi
cat > "$BIN/relay" <<EOF
#!/bin/sh
# The relay command, installed by codex-claude-relay's install.sh. Remove it with: relay uninstall
RELAY_COMMAND=relay exec python3 "$HOME_DIR/relay.py" "\$@"
EOF
chmod +x "$BIN/relay"
printf '{"command": "%s", "profiles": [%s]}\n' "$BIN/relay" "$(printf '%s' "$PROFILES" | sed 's/,$//; s/^ //')" > "$HOME_DIR/installed.json"

say "Installed the relay: $("$BIN/relay" version)"
say "  command: $BIN/relay"
[ -n "$PROFILES" ] && say "  added $BIN to the Terminal's search list; open a new Terminal window to use \`relay\`"

# 3. Optionally tell Claude and Codex about it, so "set up the relay for this project" works anywhere.
TEACH="${RELAY_TEACH:-ask}"
if [ "$TEACH" = ask ]; then
  if (exec </dev/tty) 2>/dev/null; then
    printf 'Add a short note about the relay to Claude'"'"'s and Codex'"'"'s general instructions,\nso "set up the relay for this project" works in any project? [Y/n] '
    read -r reply </dev/tty || reply=n
    case "$reply" in ''|y|Y|yes|Yes) TEACH=yes ;; *) TEACH=no ;; esac
  else
    TEACH=no
  fi
fi
if [ "$TEACH" = yes ]; then "$BIN/relay" teach
else say "To let Claude and Codex know about the relay in every project, run: relay teach"; fi

say ""
say "Next, in your project folder: relay init   (6 quick questions), then: relay run --task \"...\""
say "Or tell Claude or Codex: \"Set up the relay for this project.\""
