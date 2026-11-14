#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
exec sh kit/python.sh -B -m kit.project_docs init
