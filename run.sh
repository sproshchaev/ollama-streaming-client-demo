#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
shasum -a 256 -c fixtures/SHA256SUMS
actual=$(python3 -B experiment.py)
printf '%s\n' "$actual" | diff -u expected.txt -
printf '%s\n' "$actual"
