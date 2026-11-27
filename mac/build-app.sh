#!/bin/bash
# Build Assistant.app into mac/build/. Pass --install to copy it to ~/Applications.
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release
bin="$(swift build -c release --show-bin-path)"
app=build/Assistant.app
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Frameworks" "$app/Contents/Resources"
cp "$bin/AssistantBar" "$app/Contents/MacOS/"
cp "$bin"/lib*.dylib "$app/Contents/Frameworks/"
for bundle in "$bin"/*.bundle; do cp -R "$bundle" "$app/Contents/Resources/"; done
install_name_tool -add_rpath "@executable_path/../Frameworks" "$app/Contents/MacOS/AssistantBar"
cp Info.plist "$app/Contents/"
codesign --force --deep --sign - "$app"
if [[ "${1:-}" == "--install" ]]; then
  mkdir -p ~/Applications
  rm -rf ~/Applications/Assistant.app
  cp -R "$app" ~/Applications/
  echo "Installed ~/Applications/Assistant.app"
else
  echo "Built $PWD/$app"
fi
