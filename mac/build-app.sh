#!/bin/bash
# Build Assistant.app into mac/build/. Pass --install to copy it to ~/Applications.
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release
app=build/Assistant.app
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp "$(swift build -c release --show-bin-path)/AssistantBar" "$app/Contents/MacOS/"
cp Info.plist "$app/Contents/"
codesign --force --sign - "$app"
if [[ "${1:-}" == "--install" ]]; then
  mkdir -p ~/Applications
  rm -rf ~/Applications/Assistant.app
  cp -R "$app" ~/Applications/
  echo "Installed ~/Applications/Assistant.app"
else
  echo "Built $PWD/$app"
fi
