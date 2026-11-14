#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$root"
exec sh "$root/kit/python.sh" -m kit.run_agent --project-default "$@"
