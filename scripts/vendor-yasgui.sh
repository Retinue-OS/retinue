#!/bin/sh
# Fetch YASGUI — the SPARQL editor and result viewer of the dashboard's
# /sparql page (webapp/sparql.html, webapp/components/sparql.js) — into a
# directory the web gateway serves.
#
# The Dockerfile runs this at build time into /workspace/webapp/vendor/yasgui.
# A checkout served directly (WEBAPP_DIR pointing at the repo) needs it once:
#
#   scripts/vendor-yasgui.sh webapp/vendor/yasgui
#
# Vendored, never loaded from a CDN: the dashboard makes no third-party
# requests, and the page that queries the whole personal store is the last one
# that should announce itself to one. Pinned by version and by the tarball's
# sha256 — the same bytes the npm registry's sha512 integrity names — so a
# bump changes both lines below, and with them the Dockerfile layer that runs
# this script. Only the built bundle, its stylesheet and their licences are
# kept.
set -eu

VERSION=4.2.28
SHA256=d8935d15c4d6352451d78725b2dd4cf4acb9fcd26162fa27f1e81347e2574241

DEST=${1:?usage: vendor-yasgui.sh <destination directory>}
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

curl -fsSL "https://registry.npmjs.org/@triply/yasgui/-/yasgui-$VERSION.tgz" -o "$tmp"
if command -v sha256sum >/dev/null 2>&1; then
  echo "$SHA256  $tmp" | sha256sum -c - >/dev/null
else
  echo "$SHA256  $tmp" | shasum -a 256 -c - >/dev/null
fi

mkdir -p "$DEST"
tar -xzf "$tmp" -C "$DEST" --strip-components=2 \
  package/build/yasgui.min.js package/build/yasgui.min.css package/build/yasgui.min.js.LICENSE.txt
tar -xzf "$tmp" -C "$DEST" --strip-components=1 package/LICENSE.txt
echo "YASGUI $VERSION -> $DEST"
