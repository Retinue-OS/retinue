#!/usr/bin/env bash
"""Generate a one-time CA for the egress-audit MITM proxy.

Run this once per deployment (on the host). The generated material is
intentionally git-ignored: the private key must never be committed.

Outputs (under deploy/egress-audit/certs/):
  mitmproxy-ca.pem        combined key+cert in mitmproxy confdir format
  mitmproxy-ca-cert.pem   certificate in PEM format
  mitmproxy-ca-cert.cer   certificate in DER format
  egress-ca-cert.pem      alias of the certificate for client trust mounts
  egress-ca-key.pem       private key

The mitmproxy confdir is mounted read-write, so mitmproxy can derive its
other CA files from mitmproxy-ca.pem.
"""
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/egress-audit/certs"
mkdir -p "$DIR"

if [[ -f "$DIR/mitmproxy-ca.pem" ]]; then
  echo "CA already exists at $DIR/mitmproxy-ca.pem"
  echo "Remove the certs directory first if you want to regenerate."
  exit 0
fi

openssl req -x509 -newkey rsa:2048 \
  -keyout "$DIR/egress-ca-key.pem" \
  -out "$DIR/egress-ca-cert.pem" \
  -days 3650 -nodes \
  -subj "/CN=Retinue Egress Audit CA/O=Retinue" \
  -addext "subjectKeyIdentifier=hash" \
  -addext "authorityKeyIdentifier=keyid:always,issuer" \
  -addext "basicConstraints=critical,CA:TRUE"

# mitmproxy expects the combined key+cert as mitmproxy-ca.pem in its confdir.
cat "$DIR/egress-ca-key.pem" "$DIR/egress-ca-cert.pem" > "$DIR/mitmproxy-ca.pem"
cp "$DIR/egress-ca-cert.pem" "$DIR/mitmproxy-ca-cert.pem"
openssl x509 -in "$DIR/egress-ca-cert.pem" -outform DER -out "$DIR/mitmproxy-ca-cert.cer"

chmod 600 "$DIR/egress-ca-key.pem" "$DIR/mitmproxy-ca.pem"
chmod 644 "$DIR/egress-ca-cert.pem" "$DIR/mitmproxy-ca-cert.pem" "$DIR/mitmproxy-ca-cert.cer"

echo "Egress audit CA generated in $DIR"
echo ""
echo "Next steps:"
echo "  1. Mount $DIR as the mitmproxy confdir (see docker-compose.yml)."
echo "  2. Trust $DIR/egress-ca-cert.pem in clients that route through the proxy."
