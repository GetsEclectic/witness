#!/usr/bin/env bash
# Build mac/witness-audiotap as a universal (arm64+x86_64) binary, ad-hoc
# signed. Run from the repo root or from this directory. Maintainer use only;
# end users get the prebuilt binary committed at mac/witness-audiotap.
#
# Requires Xcode Command Line Tools (`xcode-select --install`).
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v swiftc >/dev/null 2>&1; then
    echo "swiftc not found. Install Xcode Command Line Tools: xcode-select --install" >&2
    exit 1
fi

mkdir -p .build
SRC=witness-audiotap.swift
OUT=witness-audiotap

# Pin deployment target to the lowest macOS that supports CATapDescription
# (Process Tap API landed in macOS 14.2).
TARGET_MIN=14.2

echo "[1/3] swiftc arm64-apple-macos${TARGET_MIN}"
swiftc -O -target arm64-apple-macos${TARGET_MIN}  -o .build/audiotap-arm64  "$SRC"

echo "[2/3] swiftc x86_64-apple-macos${TARGET_MIN}"
swiftc -O -target x86_64-apple-macos${TARGET_MIN} -o .build/audiotap-x86_64 "$SRC"

echo "[3/3] lipo + ad-hoc codesign"
lipo -create .build/audiotap-arm64 .build/audiotap-x86_64 -output "$OUT"
codesign --sign - --force --options runtime "$OUT"

echo "built: $(pwd)/$OUT"
file "$OUT"
codesign -dv "$OUT" 2>&1 | sed 's/^/  /'
