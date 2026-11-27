#!/bin/bash
# Sign an app bundle with the "Assistant Local Signing" certificate when it exists,
# so its signature stays the same across builds. Without it, sign ad hoc.
# Usage: ./sign-app.sh APP   Set ASSISTANT_SIGN_KEYCHAIN to look in one keychain only.
set -euo pipefail
app="$1"
name="Assistant Local Signing"
find=(security find-identity -p codesigning)
keychain=()
if [[ -n "${ASSISTANT_SIGN_KEYCHAIN:-}" ]]; then
  find+=("$ASSISTANT_SIGN_KEYCHAIN")
  keychain=(--keychain "$ASSISTANT_SIGN_KEYCHAIN")
fi
identities="$("${find[@]}")"
hash="$(sed -nE "s/^ *[0-9]+\) ([0-9A-F]{40}) \"$name\".*/\1/p" <<<"$identities" | head -n 1)"
if [[ -n "$hash" ]] && codesign --force --deep --sign "$hash" ${keychain[@]+"${keychain[@]}"} "$app"; then
  echo "Signed with \"$name\""
  exit 0
fi
codesign --force --deep --sign - "$app"
if [[ -n "$hash" ]]; then
  echo "Signed ad hoc: codesign couldn't sign with \"$name\"; see its error above." >&2
else
  echo "Signed ad hoc: macOS asks for microphone access again after each build. Run ./make-signing-cert.sh once to stop that." >&2
fi
