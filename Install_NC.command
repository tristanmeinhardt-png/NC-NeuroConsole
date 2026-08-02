#!/bin/sh
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if command -v python3.12 >/dev/null 2>&1; then
  DEFAULT_PYTHON=python3.12
else
  DEFAULT_PYTHON=python3
fi
PYTHON_BIN=${PYTHON_BIN:-$DEFAULT_PYTHON}
"$PYTHON_BIN" "$SCRIPT_DIR/install_nc.py" "$@"
RESULT=$?
if [ -t 0 ]; then
  printf '\nPress Return to close this window...'
  read -r _NC_INSTALL_REPLY
fi
exit "$RESULT"
