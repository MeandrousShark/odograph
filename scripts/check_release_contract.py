#!/usr/bin/env python3
"""Validate the version identities that must agree before a release tag ships."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from release_notes import HEADING_RE, ReleaseNotesError, extract_release_notes


# A release whose db image differs from the previous release's names the kind
# of change in its changelog section, in one of these phrases.
DB_IMAGE_REBUILD_PHRASE = "rebuilt with unchanged versions"
DB_IMAGE_MIGRATION_PHRASE = "database-image migration"


class ReleaseContractError(ValueError):
    pass


def _service_image(compose_text: str, service: str) -> str:
    in_service = False
    for line in compose_text.splitlines():
        if re.fullmatch(r"  [A-Za-z0-9_-]+:", line):
            in_service = line == f"  {service}:"
            continue
        if in_service:
            match = re.fullmatch(r"    image:\s*(\S+)\s*", line)
            if match:
                return match.group(1)
    raise ReleaseContractError(f"compose.yaml has no {service} image")


def _app_image(compose_text: str) -> str:
    return _service_image(compose_text, "app")


def _previous_release(changelog: str, tag: str) -> str | None:
    """The release whose changelog section follows this tag's, if any."""
    version = tag.removeprefix("v")
    found = False
    for line in changelog.splitlines():
        heading = HEADING_RE.fullmatch(line)
        if not heading:
            continue
        if found and re.fullmatch(r"\d+\.\d+\.\d+", heading.group("version")):
            return f"v{heading.group('version')}"
        found = found or heading.group("version") == version
    return None


def _validate_db_image_change(root: Path, changelog: str, tag: str, body: str) -> None:
    previous = _previous_release(changelog, tag)
    if previous is None:
        return
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{previous}^{{commit}}:compose.yaml"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseContractError(
            f"cannot read compose.yaml at the previous release tag {previous}; "
            "check out full history with tags"
        )
    previous_image = _service_image(result.stdout, "db")
    current_image = _service_image((root / "compose.yaml").read_text(), "db")
    if previous_image == current_image:
        return
    text = " ".join(body.split()).lower()
    if DB_IMAGE_REBUILD_PHRASE not in text and DB_IMAGE_MIGRATION_PHRASE not in text:
        raise ReleaseContractError(
            f"the db image changed since {previous}, so the {tag} changelog section "
            f"must say either {DB_IMAGE_REBUILD_PHRASE!r} or {DB_IMAGE_MIGRATION_PHRASE!r}"
        )


def validate_release_contract(root: Path, tag: str, image: str) -> None:
    changelog = (root / "CHANGELOG.md").read_text()
    notes = extract_release_notes(changelog, tag)
    _validate_db_image_change(root, changelog, tag, notes.body)

    expected_image = f"{image}:{tag}"
    actual_image = _app_image((root / "compose.yaml").read_text())
    if actual_image != expected_image:
        raise ReleaseContractError(
            f"compose app image is {actual_image!r}, expected {expected_image!r}"
        )

    readme = (root / "README.md").read_text()
    install_contracts = (
        "git checkout vX.Y.Z",
        "git clone --branch vX.Y.Z --depth 1 https://github.com/MeandrousShark/odograph.git",
    )
    if not any(contract in readme for contract in install_contracts):
        raise ReleaseContractError(
            "README.md is missing the release install contract: "
            "an exact release checkout or pinned clone"
        )
    if "exact release tag pinned by the checked-out release" not in readme:
        raise ReleaseContractError(
            "README.md is missing the release install contract: "
            "exact release tag pinned by the checked-out release"
        )
    if re.search(r"ghcr\.io/[^\s`]+:latest", readme, re.IGNORECASE):
        raise ReleaseContractError("README.md tells operators to use a latest image")

    dockerfile = (root / "Dockerfile").read_text()
    for contract in ("ARG VERSION=dev", "ENV APP_VERSION=$VERSION"):
        if contract not in dockerfile:
            raise ReleaseContractError(
                f"Dockerfile is missing the runtime version contract: {contract}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        validate_release_contract(args.root, args.tag, args.image)
    except (OSError, ReleaseContractError, ReleaseNotesError) as exc:
        raise SystemExit(f"release contract error: {exc}") from exc
    print(f"Release contract valid for {args.image}:{args.tag}")


if __name__ == "__main__":
    main()
