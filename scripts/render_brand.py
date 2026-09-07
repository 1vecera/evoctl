"""Generate repository artwork from shared OKLCH tokens and verify committed asset provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


def srgb(color: list[float]) -> str:
    """Convert perceptual OKLCH design tokens to portable SVG sRGB colors."""
    lightness, chroma, hue = color
    axis_a, axis_b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    cone_l = (lightness + 0.3963377774 * axis_a + 0.2158037573 * axis_b) ** 3
    cone_m = (lightness - 0.1055613458 * axis_a - 0.0638541728 * axis_b) ** 3
    cone_s = (lightness - 0.0894841775 * axis_a - 1.2914855480 * axis_b) ** 3
    channels = [
        4.0767416621 * cone_l - 3.3077115913 * cone_m + 0.2309699292 * cone_s,
        -1.2684380046 * cone_l + 2.6097574011 * cone_m - 0.3413193965 * cone_s,
        -0.0041960863 * cone_l - 0.7034186147 * cone_m + 1.7076147010 * cone_s,
    ]
    encoded = [12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055 for value in channels]
    return "#" + "".join(f"{round(max(0, min(1, value)) * 255):02x}" for value in encoded)


def artwork(tokens: dict, social: bool = False) -> str:
    """Compose a legible product cover with a concrete CLI-to-MCP relationship."""
    colors = {name: srgb(value) for name, value in tokens["colors"].items()}
    height = 640 if social else 496
    shift = 64 if social else 0
    radius, unit = tokens["radius"], tokens["space"]
    display, mono = tokens["fonts"]["display"], tokens["fonts"]["mono"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="{height}" viewBox="0 0 1280 {height}">',
        "<title>evoctl — Your WhatsApp control room. CLI and three MCP tools for Evolution API.</title>",
        f'<rect width="1280" height="{height}" rx="{radius}" fill="{colors["canvas"]}"/>',
        f'<g transform="translate(0 {shift})" font-family="{display}">',
    ]

    def text(
        x: int, y: int, value: str, size: int = 22, tone: str = "text", weight: int = 400, code: bool = False
    ) -> None:
        """Append escaped text using semantic color and typography tokens."""
        family = mono if code else display
        parts.append(
            f'<text x="{x}" y="{y}" fill="{colors[tone]}" font-family="{family}" '
            f'font-size="{size}" font-weight="{weight}">{escape(value)}</text>'
        )

    parts.append(
        f'<path d="M56 65 L75 82 L56 99 M90 99 H114" fill="none" stroke="{colors["brand"]}" '
        'stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>'
    )
    text(136, 105, "evoctl", 60, weight=700)
    text(56, 192, "Your WhatsApp", 52, weight=700)
    text(56, 252, "control room.", 52, weight=700)
    text(58, 300, "Evolution API. Local, HTTPS, or SSH.", 23, tone="muted")
    text(58, 370, "CLI + MCP", 19, tone="brand", code=True)
    text(238, 370, "3 tools", 19, code=True)
    text(398, 370, "182 routes", 19, code=True)
    parts.append(
        f'<rect x="638" y="56" width="586" height="332" rx="{radius}" fill="{colors["panel"]}" '
        f'stroke="{colors["border"]}"/>'
    )
    text(666, 92, "ONE PROFILE. EVERY WORKFLOW.", 15, tone="muted", code=True)
    parts.append(f'<path d="M638 112 H1224" stroke="{colors["border"]}"/>')
    text(668, 153, "$ evoctl status", 21, code=True)
    text(668, 192, '$ evoctl contacts search "Alex"', 21, code=True)
    text(668, 231, "$ evoctl messages read JID", 21, code=True)
    parts.append(f'<path d="M668 266 H1194" stroke="{colors["border"]}"/>')
    text(668, 304, "discover", 22, tone="brand", code=True)
    text(804, 304, "→", 22, tone="muted")
    text(844, 304, "read", 22, tone="brand", code=True)
    text(914, 304, "→", 22, tone="muted")
    text(954, 304, "write", 22, tone="brand", code=True)
    text(668, 347, "Schemas when you need them.", 20, tone="muted")
    parts.append(f'<path d="M56 424 H1224" stroke="{colors["border"]}"/>')
    text(56, 462, "MESSAGING · PAIRING · REMOTE HEALTH", 16, tone="muted", code=True)
    text(880, 462, "OPEN SOURCE / MIT", 16, tone="brand", code=True)
    if social:
        text(56, 512 + unit, "github.com/1vecera/evoctl", 19, tone="muted", code=True)
    return "\n".join([*parts, "</g></svg>", ""])


def main() -> None:
    """Render PNG previews with librsvg, or check source and raster hashes without native render dependencies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    directory = Path("docs/assets")
    tokens = json.loads((directory / "tokens.json").read_text())
    fingerprints = {}
    for name, social in (("hero", False), ("social", True)):
        source = artwork(tokens, social)
        svg, png = directory / f"{name}.svg", directory / f"{name}.png"
        if arguments.check:
            if svg.read_text() != source:
                raise SystemExit(f"Regenerate {svg} from the shared design tokens.")
        else:
            svg.write_text(source)
            subprocess.run(["rsvg-convert", str(svg), "-o", str(png)], check=True)
        fingerprints[name] = {
            "svg": hashlib.sha256(source.encode()).hexdigest(),
            "png": hashlib.sha256(png.read_bytes()).hexdigest(),
        }
    metadata = directory / "generated.json"
    if arguments.check:
        if json.loads(metadata.read_text()) != fingerprints:
            raise SystemExit("Artwork source or raster differs from its recorded generation.")
    else:
        metadata.write_text(json.dumps(fingerprints, indent=2) + "\n")
    print(json.dumps({"assets": list(fingerprints), "verified": arguments.check}))


if __name__ == "__main__":
    main()
