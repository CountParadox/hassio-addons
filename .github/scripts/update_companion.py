"""Bump the Companion add-on to the latest stable upstream release.

Run from the repository root. Prints nothing and changes nothing when the add-on
is already current. Writes `version=<x.y.z>` to $GITHUB_OUTPUT when it updates.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

UPSTREAM = "bitfocus/companion"
IMAGE = "bitfocus/companion/companion"
ARCHITECTURES = {"amd64", "arm64"}
ADDON = Path("companion")
STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _get(url: str, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def latest_stable_tag() -> str:
    """Highest vX.Y.Z release that is not a draft or pre-release."""
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    releases = _get(
        f"https://api.github.com/repos/{UPSTREAM}/releases?per_page=50", headers
    )
    tags = [
        r["tag_name"]
        for r in releases
        if not r["draft"] and not r["prerelease"] and STABLE_TAG.match(r["tag_name"])
    ]
    if not tags:
        sys.exit("No stable upstream release found")
    return max(tags, key=version_key)


def version_key(tag: str) -> tuple[int, ...]:
    """Companion version as a tuple; an add-on packaging revision (5.1.3-1) is ignored."""
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-\d+)?$", tag)
    if not match:
        sys.exit(f"Unexpected version: {tag}")
    return tuple(int(part) for part in match.groups())


def image_architectures(tag: str) -> set[str]:
    """Architectures published for an image tag (empty if the tag does not exist)."""
    token = _get(f"https://ghcr.io/token?scope=repository:{IMAGE}:pull")["token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.oci.image.index.v1+json, "
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
    try:
        manifest = _get(f"https://ghcr.io/v2/{IMAGE}/manifests/{tag}", headers)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return set()
        raise
    return {
        m["platform"]["architecture"]
        for m in manifest.get("manifests", [])
        if m.get("platform", {}).get("os") == "linux"
    }


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        sys.exit(f"Could not update {path}")
    path.write_text(new_text, encoding="utf-8", newline="\n")


def main() -> None:
    config = ADDON / "config.yaml"
    match = re.search(
        r'^version:\s*"?([^"\s]+)"?\s*$', config.read_text(encoding="utf-8"), re.MULTILINE
    )
    if not match:
        sys.exit("No version in companion/config.yaml")
    current = match.group(1)

    tag = latest_stable_tag()
    version = tag.removeprefix("v")
    if version_key(tag) <= version_key(current):
        print(f"Add-on is current ({current}); latest upstream is {tag}")
        return

    missing = ARCHITECTURES - image_architectures(tag)
    if missing:
        # The release exists but its image is not (fully) published yet. Try again next run.
        print(f"Image for {tag} is not ready (missing: {', '.join(sorted(missing))})")
        return

    replace_once(config, r'^version:.*$', f'version: "{version}"')
    replace_once(
        ADDON / "Dockerfile",
        r"^ARG COMPANION_VERSION=.*$",
        f"ARG COMPANION_VERSION={tag}",
    )
    changelog = ADDON / "CHANGELOG.md"
    entry = (
        f"## {version}\n\n"
        f"- Companion [{tag}](https://github.com/{UPSTREAM}/releases/tag/{tag}).\n\n"
    )
    text = changelog.read_text(encoding="utf-8")
    head, sep, rest = text.partition("\n## ")
    changelog.write_text(
        f"{head}\n{entry}## {rest}" if sep else f"{text.rstrip()}\n\n{entry}",
        encoding="utf-8",
        newline="\n",
    )

    print(f"Updated add-on from {current} to {version}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"version={version}\n")


if __name__ == "__main__":
    main()
