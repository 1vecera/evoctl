# Repository artwork

`tokens.json` owns perceptual OKLCH colors, semantic roles, typography, and geometry. `scripts/render_brand.py` generates the SVG sources, PNG exports, and provenance hashes. The hero works on both light and dark GitHub pages. `social.png` is the repository's social-preview artwork.

Render with `uv run scripts/render_brand.py`. It needs librsvg's `rsvg-convert`, Liberation Sans, and JetBrainsMono Nerd Font installed locally. Fonts are used during rendering and are not redistributed. Verify committed artwork with `uv run scripts/render_brand.py --check`; verification needs only Python and runs in CI. Inspect both PNGs after changing tokens or layout.

Terminal commands in the artwork are examples, with `JID` as a recipient placeholder. No personal conversations, credentials, or live identifiers appear in the images.
