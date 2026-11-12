#!/bin/sh
# Point git at the versioned hooks/ directory (run once per clone).
cd "$(dirname "$0")/.." && git config core.hooksPath hooks && echo "hooks installed: $(git config core.hooksPath)"
