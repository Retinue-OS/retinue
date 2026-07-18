#!/usr/bin/env bash
# Single-file QLever service.
#
# Builds an index from $INPUT_FILE on first start only — the index is
# persisted in /index (mount a volume there). On subsequent starts, when
# the marker file is present, the server boots directly without re-indexing.
#
# To force a rebuild (e.g. after the input file changes):
#   rm -rf /index/*    # inside the container
#   (then restart the container)

set -euo pipefail

INPUT_FILE="${INPUT_FILE:?INPUT_FILE must be set}"
PORT="${PORT:-7001}"
INDEX_NAME="store"
INDEX_DIR="/index"
MARKER="${INDEX_DIR}/${INDEX_NAME}.index.meta-data.json"

log() { echo "[qlever-static] $*" >&2; }

cd "${INDEX_DIR}"

# Decompress .gz input to /tmp so qlever-index receives a plain .nt file.
# The data volume is read-only, so we cannot decompress in place.
if [[ "${INPUT_FILE}" == *.gz ]]; then
    DECOMPRESSED="/tmp/$(basename "${INPUT_FILE%.gz}")"
    if [[ ! -f "${DECOMPRESSED}" ]]; then
        log "Decompressing ${INPUT_FILE} to ${DECOMPRESSED} ..."
        gunzip -c "${INPUT_FILE}" > "${DECOMPRESSED}"
        log "Decompression done."
    else
        log "Using cached decompressed file at ${DECOMPRESSED}."
    fi
    INPUT_FILE="${DECOMPRESSED}"
fi

if [[ -f "${MARKER}" ]]; then
    log "Index already present at ${INDEX_DIR} — skipping build."
else
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
