#!/bin/bash
# Sign an app bundle with the "Assistant Local Signing" certificate when it exists,
# so its signature stays the same across builds. Without it, sign ad hoc.
# Usage: ./sign-app.sh APP   Set ASSISTANT_SIGN_KEYCHAIN to look in one keychain only.
set -euo pipefail
app="$1"
name="Assistant Local Signing"
find=(security find-identity -p codesigning)
sign=(codesign --force --deep --sign "$name")
if [[ -n "${ASSISTANT_SIGN_KEYCHAIN:-}" ]]; then
  find+=("$ASSISTANT_SIGN_KEYCHAIN")
  sign+=(--keychain "$ASSISTANT_SIGN_KEYCHAIN")
fi
identities="$("${find[@]}")"
if grep -q "\"$name\"" <<<"$identities"; then
  "${sign[@]}" "$app"
  echo "Signed with \"$name\""
else
  codesign --force --deep --sign - "$app"
  echo "Signed ad hoc: macOS asks for microphone access again after each build. Run ./make-signing-cert.sh once to stop that." >&2
fi
