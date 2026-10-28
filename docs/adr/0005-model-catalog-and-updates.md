# ADR-0005: Versioned JSON catalog, SHA-256 verified archives, atomic installs

**Status:** Accepted · **Date:** 2026-10-28

## Context
Users must be able to install, switch and update models without a new
application release; downloads are hundreds of MB to 1.7 GB and must survive
flaky connections; a corrupted or malicious archive must never be used.

## Decision
- `greffier/data/catalog.json` (schema 1, `revision`, `min_app_version`) lists
  each model: archive URL (sherpa-onnx GitHub releases), size, **SHA-256**,
  archive root, role → file mapping, family, languages, resource hints.
- The same file on the main branch is fetched daily; the catalog with the
  highest `revision` wins. Updating a model = installing its new `revision`.
- Installs: resumable download (HTTP Range, `.part` + sidecar), SHA-256 and
  size verification, extraction that rejects absolute paths, `..`, links and
  devices, staging directory, atomic rename, manifest with the installed spec.
- `tools/catalog_tool.py` produces and checks entries for maintainers.

## Options considered
| Option | Updates without release | Integrity | Effort |
|---|---|---|---|
| **Own JSON catalog + SHA-256 (chosen)** | yes | strong (hash pinned by maintainers) | low |
| Hugging Face Hub API | yes | LFS hashes, but HF unreachable in some networks, gated repos | medium |
| Hard-coded URLs in code | no | weak | low |

## Consequences
- A compromised catalog URL could point to other files, but HTTPS + pinned
  hashes reviewed in pull requests limit the risk; signing the catalog is a
  possible follow-up.
- Installed models keep working if they disappear from the catalog ("orphaned").
