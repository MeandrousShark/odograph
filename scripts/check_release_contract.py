#!/usr/bin/env python3
"""Validate the version identities that must agree before a release tag ships."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from release_notes import ReleaseNotesError, extract_release_notes


class ReleaseContractError(ValueError):
    pass


def _app_image(compose_text: str) -> str:
    in_app = False
    for line in compose_text.splitlines():
        if re.fullmatch(r"  [A-Za-z0-9_-]+:", line):
            in_app = line == "  app:"
            continue
        if in_app:
            match = re.fullmatch(r"    image:\s*(\S+)\s*", line)
            if match:
                return match.group(1)
    raise ReleaseContractError("compose.yaml has no app image")


def validate_release_contract(root: Path, tag: str, image: str) -> None:
    changelog = (root / "CHANGELOG.md").read_text()
    extract_release_notes(changelog, tag)

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
    if "immutable image tag pinned by the checked-out release" not in readme:
        raise ReleaseContractError(
            "README.md is missing the release install contract: "
            "immutable image tag pinned by the checked-out release"
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
