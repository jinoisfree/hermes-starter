#!/bin/bash
set -eu
STARTER_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$STARTER_DIR/install.py" "$@"
