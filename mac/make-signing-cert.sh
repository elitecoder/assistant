#!/bin/bash
# Create the "Assistant Local Signing" certificate once. build-app.sh then signs
# every build with it, so macOS keeps microphone and speech access across rebuilds.
# Usage: ./make-signing-cert.sh [--keychain PATH]   (default: your login keychain)
# The first signing shows a keychain prompt: enter your password and select Always Allow.
set -euo pipefail
name="Assistant Local Signing"
keychain="$HOME/Library/Keychains/login.keychain-db"
if [[ "${1:-}" == "--keychain" ]]; then keychain="$2"; fi

identities="$(security find-identity -p codesigning "$keychain")"
if grep -q "\"$name\"" <<<"$identities"; then
  echo "\"$name\" is already in $keychain"
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cat > "$work/cert.cnf" <<EOF
[req]
distinguished_name = dn
prompt = no
x509_extensions = ext
[dn]
CN = $name
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF
/usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$work/cert.cnf" \
  -keyout "$work/key.pem" -out "$work/cert.pem" 2>/dev/null
/usr/bin/openssl rsa -in "$work/key.pem" -out "$work/rsa.pem" 2>/dev/null
security import "$work/rsa.pem" -k "$keychain" -t priv -f openssl -T /usr/bin/codesign >/dev/null
security import "$work/cert.pem" -k "$keychain" >/dev/null
echo "Created \"$name\" in $keychain"
