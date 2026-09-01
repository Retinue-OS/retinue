# Chat composer screenshots

Illustrations for the composer change in PR #185, referenced from that PR and
kept here so the images stay reachable after the branch is gone.

Both are rendered from the real component by Playwright/Chromium at 390×844
(a phone), against the repo's own chat fixtures. The pixel figures are the text
field's measured width in that state, not estimates.

- **`composer-width.png`** — the same message in the same chat before and after.
  Four round controls (mic, paperclip, camera, send) left the field 178px; two
  leave it 270px.
- **`composer-states.png`** — what the right-hand slot holds in each state, and
  the trade-off the mic↔send swap costs: with text in the field the mic is gone,
  so dictating *into* an existing draft takes a clear (✕) first.

The camera button only exists where the file input supports `capture`, which is
true on a phone and false in desktop Chromium — so the "before" shots emulate
that, or they would show a three-control row no phone user ever sees.

Regenerate by driving the component through those states and screenshotting the
`.composer` block; there is nothing hand-drawn here.
