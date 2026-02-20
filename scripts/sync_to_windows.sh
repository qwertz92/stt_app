#!/usr/bin/env bash
# Sync the tts_app project to a Windows-native directory.
#
# Copies the tts_app repository to a Windows-native directory so the app
# can be run natively on Windows (e.g. when developing under WSL).
#
# The source directory is automatically resolved from the script's location
# (the repo root). The target defaults to $USERPROFILE/programs/tts_app.
#
# This is necessary because:
#   - The app uses Win32 APIs (hotkeys, SendInput, clipboard) that only
#     work in native Windows.
#   - Model import scripts must write to the Windows HuggingFace cache
#     (C:\Users\<user>\.cache\huggingface\hub\), not the WSL Linux cache
#     (~/.cache/huggingface/hub/).
#
# Usage:
#   ./scripts/sync_to_windows.sh                # default target
#   ./scripts/sync_to_windows.sh /mnt/d/my-app  # custom target
#   ./scripts/sync_to_windows.sh --dry-run       # show what would be copied
#
# The script uses rsync for efficient incremental sync (only copies changed files).
set -euo pipefail

# --- Resolve source directory (repo root) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Resolve Windows USERPROFILE via WSL interop ---
resolve_win_target() {
    local win_profile
    # Try to get %USERPROFILE% from Windows via cmd.exe (WSL interop).
    if command -v cmd.exe &>/dev/null; then
        win_profile="$(cmd.exe /C "echo %USERPROFILE%" 2>/dev/null | tr -d '\r')"
        if [[ -n "$win_profile" ]]; then
            # Convert Windows path (C:\Users\foo) to WSL mount (/mnt/c/Users/foo).
            local drive_letter="${win_profile%%:*}"
            drive_letter="$(echo "$drive_letter" | tr '[:upper:]' '[:lower:]')"
            local rest="${win_profile#*:}"
            rest="${rest//\\//}"
            echo "/mnt/${drive_letter}${rest}/programs/tts_app"
            return 0
        fi
    fi
    return 1
}

# --- Parse arguments ---
DRY_RUN=false
TARGET_DIR=""

for arg in "$@"; do
    case "$arg" in
        --dry-run|-n)
            DRY_RUN=true
            ;;
        --help|-h)
            echo "Usage: $(basename "$0") [--dry-run|-n] [TARGET_DIR]"
            echo ""
            echo "Sync tts_app repo to a Windows-native directory."
            echo "Default target: \$USERPROFILE/programs/tts_app"
            exit 0
            ;;
        *)
            TARGET_DIR="$arg"
            ;;
    esac
done

# Resolve target if not specified.
if [[ -z "$TARGET_DIR" ]]; then
    TARGET_DIR="$(resolve_win_target)" || {
        echo "ERROR: Could not determine Windows USERPROFILE."
        echo "Run inside WSL, or pass target dir: $0 /mnt/c/Users/you/programs/tts_app"
        exit 1
    }
fi

# --- Validate ---
if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "ERROR: Source directory not found: $SOURCE_DIR"
    exit 1
fi

# --- Excludes (not needed on Windows) ---
EXCLUDES=(
    --exclude='.git/'
    --exclude='__pycache__/'
    --exclude='.pytest_cache/'
    --exclude='.ruff_cache/'
    --exclude='.mypy_cache/'
    --exclude='*.egg-info/'
    --exclude='.venv/'
    --exclude='.uv/'
    --exclude='*.pyc'
    --exclude='.gitignore'
    --exclude='uv.lock'
)

# --- Rsync flags ---
RSYNC_FLAGS=(
    -av                # archive mode + verbose
    --delete           # mirror: remove files in target that are not in source
    "${EXCLUDES[@]}"
)

if $DRY_RUN; then
    RSYNC_FLAGS+=(--dry-run)
fi

# --- Sync ---
echo ""
echo "=== tts_app WSL -> Windows Sync ==="
echo ""
echo "  Source:  $SOURCE_DIR"
echo "  Target:  $TARGET_DIR"
echo ""

if $DRY_RUN; then
    echo "  [DRY RUN] Showing what would be copied..."
    echo ""
fi

# Ensure target directory exists.
mkdir -p "$TARGET_DIR"

START_TIME=$(date +%s)

rsync "${RSYNC_FLAGS[@]}" "$SOURCE_DIR/" "$TARGET_DIR/"

ELAPSED=$(( $(date +%s) - START_TIME ))

echo ""
if $DRY_RUN; then
    echo "  [DRY RUN] No files were actually copied."
else
    echo "  Sync complete! (${ELAPSED}s)"
fi
echo ""

# --- Post-sync reminder ---
echo "  Next steps on Windows:"
echo "    cd $(echo "$TARGET_DIR" | sed 's|/mnt/\(.\)|\U\1:|; s|/|\\|g')"
echo "    pip install -e . --no-deps   # if first time or deps changed"
echo "    python -m tts_app            # run the app"
echo ""
echo "  To import a model on Windows:"
echo "    python scripts\\import_model.py <path-to-model-folder>"
echo "    (imports into Windows HF cache: ~\\.cache\\huggingface\\hub\\)"
echo ""
