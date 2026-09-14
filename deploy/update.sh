#!/usr/bin/env bash
set -Eeuo pipefail

# Compatibility entry point for installations whose copied systemd unit still
# invokes deploy/update.sh. Keep updater logic in the repository-root script.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec "$SCRIPT_DIR/../update.sh" "$@"
