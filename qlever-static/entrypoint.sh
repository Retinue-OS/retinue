#!/usr/bin/env bash
# Single-file QLever service.
#
# Builds an index from $INPUT_FILE on first start only — the index is
# persisted in /index (mount a volume there). On subsequent starts, when
# the marker file is present, the server boots directly without re-indexing.
#
# To force a rebuild (e.g. after the input file changes):
#   rm -rf /index/*    # inside the container
#   (then recreate the container, e.g. `docker compose up -d --force-recreate <service>`)

set -euo pipefail

INPUT_FILE="${INPUT_FILE:?INPUT_FILE must be set}"
PORT="${PORT:-7001}"
INDEX_NAME="store"
INDEX_DIR="/index"
MARKER="${INDEX_DIR}/${INDEX_NAME}.index.meta-data.json"

log() { echo "[qlever-static] $*" >&2; }

cd "${INDEX_DIR}"

if [[ -f "${MARKER}" ]]; then
    log "Index already present at ${INDEX_DIR} — skipping build."
else
    DECOMPRESSED=""
    # Decompress .gz input to /tmp so qlever-index receives a plain .nt file.
    # The data volume is read-only, so we cannot decompress in place. Always
    # decompress fresh here — do not reuse a leftover file from a previous
    # run — so a rebuild (rm -rf /index/* + recreate) reads the current
    # source instead of a stale cached decompression.
    if [[ "${INPUT_FILE}" == *.gz ]]; then
        DECOMPRESSED="/tmp/$(basename "${INPUT_FILE%.gz}")"
        log "Decompressing ${INPUT_FILE} to ${DECOMPRESSED} ..."
        gunzip -c "${INPUT_FILE}" > "${DECOMPRESSED}"
        log "Decompression done."
        INPUT_FILE="${DECOMPRESSED}"
    fi
    if [[ ! -f "${INPUT_FILE}" ]]; then
        log "ERROR: input file ${INPUT_FILE} not found."
        exit 1
    fi
    log "Building index from ${INPUT_FILE} ..."
    cat > "${INDEX_NAME}.settings.json" <<'EOF'
{ "num-triples-per-batch": 500000 }
EOF
    qlever-index \
        -i "${INDEX_NAME}" \
        -s "${INDEX_NAME}.settings.json" \
        --vocabulary-type on-disk-compressed \
        -F nt \
        -f "${INPUT_FILE}"
    log "Index built."
    # Free the decompressed copy now that indexing is done — for a genome
    # this can be tens of GB sitting in the container's writable layer.
    [[ -n "${DECOMPRESSED}" ]] && rm -f "${DECOMPRESSED}"
fi

log "Starting qlever-server on port ${PORT} ..."
exec qlever-server \
    -i "${INDEX_NAME}" \
    -p "${PORT}" \
    -j 4 \
    -m 2G \
    -c 1G \
    -e 512M \
    -k 1000
