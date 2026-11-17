#!/usr/bin/env python3
"""Render or verify a Compose file attached to a GitHub release.

--mode try (the default) renders compose.try.yaml. The repository file names no
application image. The release asset names the published manifest by digest, so
pulling it can never write a local release tag.

--mode install renders compose.yaml. The asset differs from the repository file
in exactly three places: the app image names the published manifest by digest
instead of the release tag, `pull_policy: never` is gone so Compose pulls it, and
the comment above the image says so. Every `${...}` reference stays as it is.

A release asset can be replaced after publication, so verification compares it
byte for byte with a fresh render of the repository file at the release revision.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from check_release_contract import (
    INSTALL_COMPOSE,
    TRY_COMPOSE,
    TRY_IMAGE_SERVICES,
    TRY_IMAGE_VARIABLE,
    ReleaseContractError,
    _service_image,
)


DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

# The repository file's app block, which the install render replaces whole.
INSTALL_SOURCE_COMMENT = (
    "    # This canonical file never builds and never pulls: scripts/verify_image.sh\n"
    "    # pulls the exact release tag below and verifies its signature, and `up`\n"
    "    # then uses only that local image. Contributors who want a source build\n"
    "    # add compose.build.override.yml.\n"
)
INSTALL_SOURCE_BLOCK_RE = re.compile(
    re.escape(INSTALL_SOURCE_COMMENT)
    + r"    image: (?P<image>[^\s@]+:[A-Za-z0-9_.-]+)\n"
    + r"    pull_policy: never\n"
)
INSTALL_RENDERED_COMMENT = (
    "    # Pinned to the published release digest, so Compose pulls exactly that\n"
    "    # image. This route does not check its signature; scripts/verify_image.sh\n"
    "    # in a full release checkout does.\n"
)


def _pinned_image(image: str, digest: str) -> str:
    if not DIGEST_RE.fullmatch(digest):
        raise ReleaseContractError(f"{digest!r} is not a sha256 digest")
    return f"{image}@{digest}"


def _check_rendered(text: str, expected: str) -> None:
    if "ODOGRAPH_TRY_IMAGE" in text or "${" in text:
        raise ReleaseContractError(f"rendered {TRY_COMPOSE} still contains a variable")
    for service in TRY_IMAGE_SERVICES:
        actual = _service_image(text, service, TRY_COMPOSE)
        if actual != expected:
            raise ReleaseContractError(
                f"rendered {TRY_COMPOSE} {service} image is {actual!r}, expected {expected!r}"
            )


def render(text: str, image: str, digest: str) -> str:
    expected = _pinned_image(image, digest)
    if text.count(TRY_IMAGE_VARIABLE) != len(TRY_IMAGE_SERVICES):
        raise ReleaseContractError(f"{TRY_COMPOSE} does not carry the image variable as expected")
    rendered = text.replace(TRY_IMAGE_VARIABLE, expected)
    _check_rendered(rendered, expected)
    return rendered


def _install_source_parts(text: str) -> tuple[str, str]:
    """The source around its app block, which must appear once and belong to app."""
    matches = list(INSTALL_SOURCE_BLOCK_RE.finditer(text))
    if len(matches) != 1:
        raise ReleaseContractError(
            f"{INSTALL_COMPOSE} does not carry the app image block as expected"
        )
    [match] = matches
    if _service_image(text, "app", INSTALL_COMPOSE) != match.group("image"):
        raise ReleaseContractError(f"{INSTALL_COMPOSE} app image block is not under app")
    return text[:match.start()], text[match.end():]


def _check_install_rendered(text: str, source: str, expected: str) -> None:
    """Checked apart from the substitution: the source with only the three changes."""
    head, tail = _install_source_parts(source)
    if text != f"{head}{INSTALL_RENDERED_COMMENT}    image: {expected}\n{tail}":
        raise ReleaseContractError(
            f"rendered {INSTALL_COMPOSE} changes more than the app image block"
        )
    if text.count("${") != source.count("${"):
        raise ReleaseContractError(f"rendered {INSTALL_COMPOSE} lost a variable reference")
    if "pull_policy: never" in text:
        raise ReleaseContractError(f"rendered {INSTALL_COMPOSE} still never pulls")
    actual = _service_image(text, "app", INSTALL_COMPOSE)
    if actual != expected:
        raise ReleaseContractError(
            f"rendered {INSTALL_COMPOSE} app image is {actual!r}, expected {expected!r}"
        )


def render_install(text: str, image: str, digest: str) -> str:
    expected = _pinned_image(image, digest)
    _install_source_parts(text)
    rendered = INSTALL_SOURCE_BLOCK_RE.sub(
        lambda _match: f"{INSTALL_RENDERED_COMMENT}    image: {expected}\n", text, count=1
    )
    _check_install_rendered(rendered, text, expected)
    return rendered


def verify(candidate: bytes, source: str, image: str, digest: str) -> None:
    expected = render(source, image, digest).encode()
    if candidate == expected:
        return
    if b"ODOGRAPH_TRY_IMAGE" in candidate:
        raise ReleaseContractError(f"rendered {TRY_COMPOSE} still contains a variable")
    raise ReleaseContractError(
        f"{TRY_COMPOSE} differs from the render of its source: "
        f"sha256 {hashlib.sha256(candidate).hexdigest()}, "
        f"expected {hashlib.sha256(expected).hexdigest()}"
    )


def verify_install(candidate: bytes, source: str, image: str, digest: str) -> None:
    expected = render_install(source, image, digest).encode()
    if candidate == expected:
        return
    if b"pull_policy: never" in candidate:
        raise ReleaseContractError(f"rendered {INSTALL_COMPOSE} still never pulls")
    raise ReleaseContractError(
        f"{INSTALL_COMPOSE} differs from the render of its source: "
        f"sha256 {hashlib.sha256(candidate).hexdigest()}, "
        f"expected {hashlib.sha256(expected).hexdigest()}"
    )


MODES = {
    "try": (TRY_COMPOSE, render, verify),
    "install": (INSTALL_COMPOSE, render_install, verify_install),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=MODES, default="try")
    parser.add_argument("--image", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument(
        "--source", type=Path,
        help="Repository file at the release revision (default: the mode's file)",
    )
    parser.add_argument("--output", type=Path, help="Rendered file to write")
    parser.add_argument("--verify", type=Path, help="Rendered file to check instead of rendering")
    args = parser.parse_args()
    if bool(args.output) == bool(args.verify):
        parser.error("use exactly one of --output or --verify")
    if args.source is None:
        args.source = Path(MODES[args.mode][0])
    return args


def main() -> None:
    args = parse_args()
    _, render_mode, verify_mode = MODES[args.mode]
    try:
        # Bytes, so that newline translation cannot hide a difference.
        source = args.source.read_bytes().decode()
        if args.verify:
            verify_mode(args.verify.read_bytes(), source, args.image, args.digest)
        else:
            args.output.write_bytes(render_mode(source, args.image, args.digest).encode())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{args.mode} compose error: {exc}") from exc


if __name__ == "__main__":
    main()
