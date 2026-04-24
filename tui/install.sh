#!/usr/bin/env bash
# aztea-tui installer  -  installs the Aztea terminal UI
# Usage: curl -fsSL https://aztea.ai/install-tui.sh | bash
set -euo pipefail

PACKAGE="aztea-tui"
MIN_PYTHON="3.11"

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

bold "◆  Aztea TUI Installer"
echo ""

# ── Check Python ──────────────────────────────────────────────────────────────
check_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
            major=${ver%%.*}
            minor=${ver##*.}
            if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON=$(check_python 2>/dev/null) || {
    red "Python $MIN_PYTHON+ is required but not found."
    echo "Install Python from https://python.org/downloads/ and try again."
    exit 1
}

echo "Python: $($PYTHON --version)"

# ── Try pipx first, fall back to pip ──────────────────────────────────────────
if command -v pipx &>/dev/null; then
    bold "Installing via pipx…"
    pipx install "$PACKAGE" --force
    green "Done! Run: aztea-tui"

elif command -v uv &>/dev/null; then
    bold "Installing via uv…"
    uv tool install "$PACKAGE"
    green "Done! Run: aztea-tui"

else
    bold "Installing via pip (user install)…"
    "$PYTHON" -m pip install --user --upgrade "$PACKAGE"
    # Ensure user bin is on PATH
    USER_BIN=$("$PYTHON" -m site --user-base)/bin
    if [[ ":$PATH:" != *":$USER_BIN:"* ]]; then
        echo ""
        echo "Add this to your shell profile (~/.bashrc, ~/.zshrc):"
        echo "  export PATH=\"\$PATH:$USER_BIN\""
        echo ""
        echo "Then restart your shell and run: aztea-tui"
    else
        green "Done! Run: aztea-tui"
    fi
fi
