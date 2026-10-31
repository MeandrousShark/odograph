"""Checks for the public contributor documentation, links and assets."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]

PUBLIC_MARKDOWN = (
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "ai-contribution.md",
    "THIRD_PARTY_NOTICES.md",
    "CHANGELOG.md",
    "docs/accounts.md",
    "docs/backups.md",
    "docs/configuration.md",
    "docs/detection.md",
    "docs/install-compose.md",
    "docs/known-issues.md",
    "docs/oidc.md",
    "docs/osrm.md",
    "docs/owntracks.md",
    "docs/privacy.md",
    "docs/releasing.md",
    "docs/reverse-proxy.md",
    "docs/security.md",
    "docs/traccar.md",
    "docs/upgrading.md",
    "docs/usage.md",
)
PUBLIC_IMAGES = (
    "docs/images/usage-dashboard.png",
    "docs/images/usage-review.png",
    "docs/images/usage-trips.png",
    "docs/images/usage-report.png",
)
PUBLIC_TOP_LEVEL_FILES = (
    "LICENSE",
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "compose.build.override.yml",
    "compose.try.yaml",
    "compose.yaml",
    "Dockerfile",
    "pyproject.toml",
    "requirements-dev.lock",
    "requirements-dev.txt",
    "requirements.lock",
    "requirements.txt",
)
PUBLIC_FILES = frozenset((*PUBLIC_MARKDOWN, *PUBLIC_IMAGES, *PUBLIC_TOP_LEVEL_FILES))
PUBLIC_DIRECTORIES = (".github", "app", "docker", "migrations", "scripts", "static", "tests")

_LINK_START = re.compile(r"(?P<image>!?)(?:\[[^\]\n]*\])\(")
_ATX_HEADING = re.compile(r"^ {0,3}(?P<marks>#{1,6})(?:[ \t]+|$)(?P<text>.*?)\s*#*\s*$")


def _mask_code(text: str) -> str:
    """Replace fenced and inline code with spaces, preserving offsets."""
    chars = list(text)
    in_fence = False
    fence_marker = ""
    offset = 0
    for line in text.splitlines(keepends=True):
        line_without_newline = line.rstrip("\r\n")
        stripped = line_without_newline.lstrip()
        if in_fence:
            if stripped.startswith(fence_marker):
                in_fence = False
            for index in range(offset, offset + len(line_without_newline)):
                chars[index] = " "
        else:
            marker = next((value for value in ("```", "~~~") if stripped.startswith(value)), None)
            if marker:
                in_fence = True
                fence_marker = marker
                for index in range(offset, offset + len(line_without_newline)):
                    chars[index] = " "
        offset += len(line)

    masked = "".join(chars)
    chars = list(masked)
    index = 0
    while index < len(masked):
        if masked[index] != "`":
            index += 1
            continue
        end = index
        while end < len(masked) and masked[end] == "`":
            end += 1
        run = masked[index:end]
        close = masked.find(run, end)
        if close == -1:
            index = end
            continue
        for position in range(index, close + len(run)):
            if chars[position] != "\n":
                chars[position] = " "
        index = close + len(run)
    return "".join(chars)


def _link_destination_end(text: str, opening_parenthesis: int) -> int:
    depth = 0
    escaped = False
    for index in range(opening_parenthesis, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _iter_links(text: str):
    masked = _mask_code(text)
    for match in _LINK_START.finditer(masked):
        end = _link_destination_end(text, match.end() - 1)
        if end == -1:
            continue
        yield match.group("image") == "!", text[match.end() : end]


def _destination(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<"):
        close = value.find(">", 1)
        return value[1:close] if close != -1 else value
    return value.split(None, 1)[0] if value else ""


def _github_heading_slug(text: str) -> str:
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "")
    text = unicodedata.normalize("NFKC", text).lower()
    text = "".join(char for char in text if char.isalnum() or char in " _-")
    text = re.sub(r"\s+", "-", text)
    return text.strip("-")


def _heading_ids(text: str) -> set[str]:
    ids: set[str] = set()
    duplicate_counts: dict[str, int] = {}
    masked = _mask_code(text)
    for line, masked_line in zip(text.splitlines(), masked.splitlines()):
        if not _ATX_HEADING.match(masked_line):
            continue
        match = _ATX_HEADING.match(line)
        assert match is not None
        heading_text = line[match.start("text") : match.end("text")]
        slug = _github_heading_slug(heading_text)
        if not slug:
            continue
        count = duplicate_counts.get(slug, 0)
        duplicate_counts[slug] = count + 1
        ids.add(slug if count == 0 else f"{slug}-{count}")
    return ids


def _link_errors(root: Path, markdown_paths: tuple[str, ...] = PUBLIC_MARKDOWN) -> list[str]:
    errors: list[str] = []
    allowed_files = PUBLIC_FILES
    for relative in markdown_paths:
        source = root / relative
        if not source.is_file():
            errors.append(f"{relative}: public Markdown file is missing")
            continue
        text = source.read_text(encoding="utf-8")
        for is_image, raw in _iter_links(text):
            target = _destination(raw)
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue
            path_part = unquote(parsed.path)
            if path_part.startswith("/"):
                candidate = root / path_part.lstrip("/")
            elif path_part:
                candidate = source.parent / path_part
            else:
                candidate = source
            try:
                target_relative = candidate.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                target_relative = ""
            target_is_public = target_relative in allowed_files or any(
                target_relative == directory or target_relative.startswith(f"{directory}/")
                for directory in PUBLIC_DIRECTORIES
            )
            if not target_relative or not target_is_public:
                kind = "image" if is_image else "link"
                errors.append(f"{relative}: local {kind} target is not public: {target}")
                continue
            if not candidate.exists():
                errors.append(f"{relative}: local target does not exist: {target}")
                continue
            if parsed.fragment:
                if target_relative not in PUBLIC_MARKDOWN:
                    errors.append(f"{relative}: fragment target is not Markdown: {target}")
                    continue
                fragment = unquote(parsed.fragment).lower()
                if fragment not in _heading_ids(candidate.read_text(encoding="utf-8")):
                    errors.append(f"{relative}: heading anchor does not exist: {target}")
    return errors


def test_public_document_inventory_and_images_exist():
    for relative in (*PUBLIC_MARKDOWN, *PUBLIC_IMAGES):
        path = ROOT / relative
        assert path.is_file(), f"approved public path is missing: {relative}"
        if relative in PUBLIC_IMAGES:
            assert path.stat().st_size > 0, f"public image is empty: {relative}"


def test_public_markdown_local_links_and_fragments_resolve():
    assert _link_errors(ROOT) == []


def test_heading_anchor_supports_inline_code_and_duplicate_github_slugs(tmp_path):
    document = tmp_path / "guide.md"
    document.write_text("## Run `compose up`\n\n## Run compose up\n")

    assert _heading_ids(document.read_text()) == {"run-compose-up", "run-compose-up-1"}


def test_invalid_link_fixture_is_rejected_without_scanning_private_docs(tmp_path):
    """Keep the deliberately broken fixture outside the real inventory."""
    (tmp_path / "README.md").write_text("[broken](missing.md#nowhere)\n")

    assert _link_errors(tmp_path, ("README.md",)) == [
        "README.md: local link target is not public: missing.md#nowhere"
    ]


@pytest.mark.parametrize(
    "private_name",
    ["DES" + "IGN.md", "HAND" + "OFF.md", "MAINTAINER-" + "RUNBOOK.md"],
)
def test_private_documents_are_not_part_of_public_markdown_inventory(private_name):
    assert all(Path(relative).name != private_name for relative in PUBLIC_MARKDOWN)


_IDENTITY_REGEXP_FLAG = re.compile(
    r"--certificate-identity-regexp(?:=|[ \t]+|[ \t]*\\\n[ \t]*)"
    r"(?P<value>'[^'\n]*'|\"[^\"\n]*\"|[^\s'\"]+)"
)
# Wildcards that would let the owner, repository or workflow file vary.
_IDENTITY_WILDCARDS = (".*", ".+", "[^", "\\S", "\\w", "(?")


def _identity_regexp_errors(root: Path, relative_paths) -> list[str]:
    errors = []
    for relative in relative_paths:
        text = (root / relative).read_text(encoding="utf-8")
        for match in _IDENTITY_REGEXP_FLAG.finditer(text):
            raw = match.group("value")
            value = raw[1:-1] if raw[:1] in "'\"" else raw
            if not (value.startswith("^") and value.endswith("$")):
                errors.append(f"{relative}: certificate identity regexp is not anchored: {raw}")
                continue
            # Only the part before the tag separator names owner, repo and workflow.
            identity = value[1:-1].split("@", 1)[0]
            bad = [token for token in _IDENTITY_WILDCARDS if token in identity]
            if bad:
                errors.append(
                    f"{relative}: certificate identity regexp has a wildcard "
                    f"in the owner, repository or workflow file: {raw}"
                )
    return errors


def _identity_regexp_scan_paths() -> list[str]:
    paths = list(PUBLIC_MARKDOWN)
    for pattern in ("scripts/*", ".github/workflows/*.yml"):
        paths.extend(
            str(path.relative_to(ROOT)) for path in sorted(ROOT.glob(pattern)) if path.is_file()
        )
    return paths


def test_certificate_identity_regexps_are_anchored_and_name_one_workflow():
    assert _identity_regexp_errors(ROOT, _identity_regexp_scan_paths()) == []


def test_certificate_identity_regexp_scanner_rejects_loose_patterns(tmp_path):
    loose = {
        "unanchored.md": "cosign verify --certificate-identity-regexp "
        "'https://github\\.com/o/r/\\.github/workflows/release\\.yml@refs/tags/v1'\n",
        "any-owner.md": "cosign verify --certificate-identity-regexp "
        "'^https://github\\.com/.*/\\.github/workflows/release\\.yml@refs/tags/.*$'\n",
        "any-workflow.md": 'cosign verify --certificate-identity-regexp "^https://github.com/$REPOSITORY'
        '/.github/workflows/.*@refs/tags/$VERSION$"\n',
        "continued.sh": "cosign verify \\\n  --certificate-identity-regexp "
        "'^https://github\\.com/[^/]+/repo/\\.github/workflows/release\\.yml@refs/tags/v1$'\n",
    }
    for name, content in loose.items():
        (tmp_path / name).write_text(content)
    (tmp_path / "exact.md").write_text(
        "cosign verify \\\n  --certificate-identity-regexp "
        "'^https://github\\.com/Owner/repo/\\.github/workflows/release\\.yml@refs/tags/v[0-9]+\\.[0-9]+\\.[0-9]+$'\n"
    )

    errors = _identity_regexp_errors(tmp_path, [*loose, "exact.md"])

    assert [error.split(":", 1)[0] for error in errors] == list(loose)


def test_nginx_example_replaces_x_forwarded_for_with_the_connecting_address():
    """Appending keeps a client-supplied value ahead of the proxy's own."""
    appending = "$proxy_add_x_forwarded_for"
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in documents
        if appending in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

    proxy_guide = (ROOT / "docs/reverse-proxy.md").read_text(encoding="utf-8")
    nginx_block = proxy_guide.split("```nginx", 1)[1].split("```", 1)[0]
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx_block
