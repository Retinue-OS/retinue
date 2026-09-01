# Chat composer screenshots

Illustrations for the composer change in PR #185, referenced from that PR and
kept here so the images stay reachable after the branch is gone.

Both are rendered from the real component by Playwright/Chromium at 390×844
(a phone), against the repo's own chat fixtures. The pixel figures are measured
off the live elements, not estimates.

- **`composer-width.png`** — the same message in the same chat before and after.
  Four round controls (mic, paperclip, camera, send) left the field 178px and
  120px of room for the text itself; two leave it 270px and 182px.
- **`composer-states.png`** — the chat composer beside the conversation
  composer, which is the row it now matches: mic left, send right, paperclip
  inside the field. Then the states, to show that nothing swaps — the ✕ is the
  only control that comes and goes, and only when there is text to clear.

Two things worth knowing before reshooting these:

- The camera button in the "before" only exists where the file input supports
  `capture`, which is true on a phone and false in desktop Chromium. Without
  emulating it the before/after understates itself by a whole 46px control.
- A degenerate test image (a few pixels square) is rejected by the composer's
  downscale path, which is correct behaviour and not a staging bug — use a
  realistically sized image or the staged-image shot will come out empty.

Regenerate by driving the component through those states and screenshotting the
`.composer` block; there is nothing hand-drawn here.
