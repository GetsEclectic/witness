#!/usr/bin/env bash
# Build mac/WitnessSlackProbe.app (universal, ad-hoc signed). Maintainer use.
#
# Rebuilding changes the code hash, which voids the Accessibility grant:
# re-add the app in System Settings → Privacy & Security → Accessibility.
set -euo pipefail

cd "$(dirname "$0")"

mkdir -p .build WitnessSlackProbe.app/Contents/MacOS
TARGET_MIN=14.2
swiftc -O -target arm64-apple-macos${TARGET_MIN}  -o .build/slackprobe-arm64  witness-slackprobe.swift
swiftc -O -target x86_64-apple-macos${TARGET_MIN} -o .build/slackprobe-x86_64 witness-slackprobe.swift
lipo -create .build/slackprobe-arm64 .build/slackprobe-x86_64 \
    -output WitnessSlackProbe.app/Contents/MacOS/witness-slackprobe
codesign --sign - --force --options runtime WitnessSlackProbe.app

echo "built: $(pwd)/WitnessSlackProbe.app"
codesign -dv WitnessSlackProbe.app 2>&1 | sed 's/^/  /'
