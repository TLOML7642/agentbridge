#!/bin/bash
# Compatibility entry point; all credential maintenance shares the safe official CLI path.
set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/agentbridge_set_credentials.sh" "$@"
