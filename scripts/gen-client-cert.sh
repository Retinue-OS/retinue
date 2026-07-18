#!/usr/bin/env bash
# Issue a browser-installable client certificate (.p12) for the gateway's
# mutual-TLS auth. Creates the client CA on first run, then signs one client
# cert per invocation. Run this on a trusted machine that holds the CA key — NOT
# inside the agent container.
#
# Usage:
#   scripts/gen-client-cert.sh [--name NAME] [--out DIR] [--days N] [--ca-days N]
#
#   --name    Common Name / friendly name for the cert (default: hostname-user)
#   --out     Output directory (default: ./certs)
#   --days    Client cert validity in days (default: 825 — max browsers accept)
#   --ca-days CA validity in days, only used when creating the CA (default: 3650)
#
# Outputs in DIR:
#   ca.key / ca.crt        the client CA (keep ca.key OFFLINE and safe)
#   <name>.p12             import this into the browser (passphrase printed + saved)
#   <name>-passphrase.txt  the .p12 import passphrase
#
# After issuing, copy ca.crt to deploy/traefik/dynamic/retinue-client-ca.crt and
# (re)load Traefik. See deploy/traefik/README.md.
set -euo pipefail

NAME="$(hostname -s 2>/dev/null || echo client)-${USER:-user}"
OUT="./certs"
DAYS=825
CA_DAYS=3650

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --days) DAYS="$2"; shift 2 ;;
    --ca-days) CA_DAYS="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$OUT"
cd "$OUT"

# --- Client CA (created once, reused thereafter) ---
if [[ ! -f ca.key || ! -f ca.crt ]]; then
  echo "[ca] creating client CA (valid ${CA_DAYS} days) ..."
  openssl genrsa -out ca.key 4096
  chmod 600 ca.key
  openssl req -x509 -new -nodes -key ca.key -sha256 -days "$CA_DAYS" \
    -subj "/CN=Retinue Client CA/O=Retinue" -out ca.crt
else
  echo "[ca] reusing existing CA (ca.crt)"
fi

# --- Client certificate ---
echo "[cert] issuing client certificate for '${NAME}' (valid ${DAYS} days) ..."
openssl genrsa -out "${NAME}.key" 2048
chmod 600 "${NAME}.key"
openssl req -new -key "${NAME}.key" -subj "/CN=${NAME}/O=Retinue" -out "${NAME}.csr"
cat > "${NAME}.ext" <<EOF
extendedKeyUsage = clientAuth
keyUsage = critical, digitalSignature
EOF
openssl x509 -req -in "${NAME}.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days "$DAYS" -sha256 -extfile "${NAME}.ext" -out "${NAME}.crt"
rm -f "${NAME}.csr" "${NAME}.ext"

# --- PKCS#12 bundle for the browser ---
PASS="$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)"
echo "$PASS" > "${NAME}-passphrase.txt"
chmod 600 "${NAME}-passphrase.txt"
# -legacy: RC2/SHA1 encoding is the most broadly importable across browsers.
openssl pkcs12 -export -legacy \
  -inkey "${NAME}.key" -in "${NAME}.crt" -certfile ca.crt \
  -name "Retinue – ${NAME}" -passout pass:"$PASS" -out "${NAME}.p12" 2>/dev/null || \
openssl pkcs12 -export \
  -inkey "${NAME}.key" -in "${NAME}.crt" -certfile ca.crt \
  -name "Retinue – ${NAME}" -passout pass:"$PASS" -out "${NAME}.p12"
chmod 600 "${NAME}.p12"

echo
echo "Done. In ${OUT}/:"
echo "  ${NAME}.p12            -> import into your browser"
echo "  import passphrase      -> ${PASS}   (also in ${NAME}-passphrase.txt)"
echo "  ca.crt                 -> copy to deploy/traefik/dynamic/retinue-client-ca.crt"
echo
echo "Keep ca.key OFFLINE. Reload Traefik after updating the CA. See deploy/traefik/README.md."
