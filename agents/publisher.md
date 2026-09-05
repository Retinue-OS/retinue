# Publisher Instructions

## Translation manifest

The following canonical documents should be translated into the listed languages.
Re-translate when the source document changes materially (not just formatting).

| Document | de | fr | pt-BR |
|---|:---:|:---:|:---:|
| `diagnosis.md` | yes | — | — |
| `therapy/medication.md` | yes | — | — |
| `therapy/nutrition/cooking/cooking-guide.md` | yes | yes | yes |
| `therapy/nutrition/cooking/ingredient-guide.md` | yes | yes | yes |
| `therapy/nutrition/cooking/fat-bombs.md` | yes | yes | yes |

A row is a standing instruction, not a record of what exists: a document listed
here without its rendition is outstanding work. But every rendition on disk
must have a row — a translation nobody has committed to maintaining goes stale
silently. Check both directions before translating: a canonical source that has
disappeared, and a rendition with no row.

## Per-language notes

Before generating or updating any translation, read the relevant
`renditions/<lang>/notes.md` file. Corrections recorded there take precedence
over your default translation choices. Do not silently revert a correction on
re-translation.

## Terms not to translate

- Brand names: Ultrahuman, FreeStyle Libre, Coimbra
- Units: mmol/L, mg/dL, IU, mcg
- Drug names (use the name as written in the source)

## Formatting conventions

- Preserve all markdown structure (headings, tables, bullet levels)
- Preserve internal links and file references unchanged
- Do not add, remove, or reorder sections *relative to the source*. This forbids
  inventing structure, not tracking the source: when the canonical document
  gains, loses or moves a section, the renditions follow it.
- **Write each language's own characters.** Diacritics, ligatures, cedillas and
  the Greek letters used in scientific notation belong in the text as the
  language writes them — never transliterated to ASCII, never dropped. This
  holds for every language equally, including English. Content that spells a
  word without its diacritic is misspelled, not simplified.
- **File and directory names stay ASCII.** Path components are normalized to
  NFC or NFD depending on the operating system, so a non-ASCII path produces
  duplicate git entries and broken links across platforms. The content carries
  the diacritics; the path does not. A language's `notes.md` may record its own
  transliteration scheme for filenames.
