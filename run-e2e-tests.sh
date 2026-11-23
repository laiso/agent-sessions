#!/bin/sh
set -eu
cd "$(dirname "$0")"
export PYTHONPATH=src
exec python3 -m unittest discover -s tests_e2e -v
