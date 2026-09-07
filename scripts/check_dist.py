"""Verify published archives contain only intended package and project files."""

from __future__ import annotations

import json
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


def main() -> None:
    """Reject scratch files, virtual environments, and missing catalogs before distribution."""
    allowed = {
        "src",
        "tests",
        "scripts",
        "docs",
        "README.md",
        "LICENSE",
        "NOTICE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "AGENTS.md",
        "pyproject.toml",
        "uv.lock",
        "PKG-INFO",
        ".gitignore",
    }
    checked = []
    for source in sorted(Path("dist").glob("*.tar.gz")):
        with tarfile.open(source) as archive:
            names = [PurePosixPath(member.name) for member in archive.getmembers()]
            if not names or any(len(name.parts) < 2 or name.parts[1] not in allowed for name in names):
                raise ValueError("Source archive contains an unexpected top-level path.")
            if any("tmp" in name.parts or ".venv" in name.parts or ".." in name.parts for name in names):
                raise ValueError("Source archive contains scratch or traversal paths.")
            if not any(name.name == "api_catalog.json" for name in names):
                raise ValueError("Source archive is missing the API catalog.")
        checked.append(source.name)
    for wheel in sorted(Path("dist").glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            names = [PurePosixPath(name) for name in archive.namelist()]
            if not names or any(
                name.parts[0] != "evoctl" and not name.parts[0].endswith(".dist-info") for name in names
            ):
                raise ValueError("Wheel contains an unexpected top-level path.")
            if "evoctl/api_catalog.json" not in archive.namelist():
                raise ValueError("Wheel is missing the API catalog.")
        checked.append(wheel.name)
    if len(checked) < 2:
        raise ValueError("Build both the source distribution and wheel before auditing.")
    print(json.dumps({"archives_checked": checked, "unexpected_paths": 0}))


if __name__ == "__main__":
    main()
