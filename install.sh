#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
case "${1:-}" in
    --update|--preview|--help|-h) exec sh "$root/kit/python.sh" "$root/install.py" "$@" ;;
esac
exec sh "$root/kit/runtime/desktop.sh" "$root/install.py" "$@"
