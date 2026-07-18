# qlever-static

Minimal QLever service for a single, static N-Triples file. Designed for
data that rarely (if ever) changes — like a reference genome.

## What it does

On first container start, indexes `$INPUT_FILE` (default `/data/genetics.nt`)
into `/index`. On subsequent starts, when the index marker is present, the
server boots directly without re-indexing.

All triples land in the default graph — no named-graph synthesis, no
filesystem watching, no blue-green rebuild. If you need any of that, use
[qlever-dir](https://github.com/retinue-os/qlever-dir) instead.

## Environment variables

| Variable     | Default              | Purpose                       |
|--------------|----------------------|-------------------------------|
| `INPUT_FILE` | `/data/genetics.nt`  | Path to the N-Triples file    |
| `PORT`       | `7001`               | Port the SPARQL server listens on |

## Volumes

- `/data` (ro): mount the directory containing your input file
- `/index`: mount a named volume here to persist the index across restarts

## Refreshing the data

When the input file changes, the existing index is **not** invalidated
automatically. To rebuild:

```bash
docker compose exec qlever-genomics sh -c 'rm -rf /index/*'
docker compose restart qlever-genomics
```
