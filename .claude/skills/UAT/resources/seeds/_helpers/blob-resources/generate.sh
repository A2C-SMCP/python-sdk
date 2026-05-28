#!/usr/bin/env bash
# Generate blob test resources + print SHA256 for verification
set -Eeuo pipefail

OUT_DIR="${1:?usage: generate.sh <output-dir>}"
mkdir -p "$OUT_DIR"

python3 -c "print('A' * 100, end='')" > "$OUT_DIR/small.txt"
python3 -c "print('B' * 65536, end='')" > "$OUT_DIR/large.txt"

echo "small_sha256=$(shasum -a 256 "$OUT_DIR/small.txt" | cut -d' ' -f1)"
echo "large_sha256=$(shasum -a 256 "$OUT_DIR/large.txt" | cut -d' ' -f1)"
echo "small_size=$(wc -c < "$OUT_DIR/small.txt" | tr -d ' ')"
echo "large_size=$(wc -c < "$OUT_DIR/large.txt" | tr -d ' ')"
