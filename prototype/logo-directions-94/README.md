# Logo direction prototype — issue #94

Throwaway. Not part of the app; kept here as the primary source for the
decision, not folded into `main`.

**Question:** what should the `tfi_live` icon look like, and what does
`home-assistant/brands` (or its Brands Proxy equivalent) require?

**Process:** `gallery.html` renders three structurally different concepts
side by side — Live Signal (bus + broadcast arcs), Pin + Clock (real-time
arrival), Route Monogram (abstract T/L route line + vehicle dot) — each
checked at 256/48/24/16px against light and dark sidebar backdrops, plus a
spec panel of the actual brands-repo asset requirements.

**Verdict:** Concept A, "Live Signal," chosen 2026-07-15. Rasterized and
folded into `custom_components/tfi_live/brand/` on `main` as `icon.png`,
`icon@2x.png`, `logo.png`, `logo@2x.png` (commit 68f6e6c). Decision and
rationale also recorded on #82.

`icon-a.svg` / `logo-a.svg` here are the vector sources for the winning
concept, kept for reference; `gallery.html` is the full three-way pitch.
