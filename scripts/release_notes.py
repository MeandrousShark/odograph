#!/usr/bin/env python3
import argparse
import re
from dataclasses import dataclass
from pathlib import Path


TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")
HEADING_RE = re.compile(r"^## \[(?P<version>[^]]+)](?: - \d{4}-\d{2}-\d{2})?\s*$")
ACCEPTANCE_TOKEN = "Security scan acceptance"
ACCEPTANCE_RE = re.compile(
    r"^- \*\*Security scan acceptance \("
    r"(?P<scanner>pip-audit|Trivy (?P<scope>linux/amd64|linux/arm64|all))"
    r"\):\*\* `(?P<vulnerability>[^`]+)` — (?P<reason>\S(?:.*\S)?)$"
)
VULNERABILITY_RE = re.compile(
    r"^(?:CVE-\d{4}-\d{4,}|PYSEC-\d{4}-\d+|GHSA-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}-[0-9A-Za-z]{4})$"
)


class ReleaseNotesError(ValueError):
    pass


@dataclass(frozen=True)
class Acceptance:
    scanner: str
    vulnerability: str
    reason: str
    scope: str | None = None


@dataclass(frozen=True)
class ReleaseNotes:
    body: str
    acceptances: tuple[Acceptance, ...]

    @property
    def pip_audit_ignores(self) -> tuple[str, ...]:
        return _unique(
            acceptance.vulnerability
            for acceptance in self.acceptances
            if acceptance.scanner == "pip-audit"
        )

    def trivy_ignores(self, platform: str) -> tuple[str, ...]:
        if platform not in {"linux/amd64", "linux/arm64"}:
            raise ReleaseNotesError(f"unsupported Trivy platform: {platform}")
        return _unique(
            acceptance.vulnerability
            for acceptance in self.acceptances
            if acceptance.scanner == "Trivy"
            and acceptance.scope in {platform, "all"}
        )


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def extract_release_notes(changelog: str, tag: str) -> ReleaseNotes:
    match = TAG_RE.fullmatch(tag)
    if not match:
        raise ReleaseNotesError(f"release tag is not semantic-version shaped: {tag}")

    version = match.group("version")
    lines = changelog.splitlines()
    start = None
    for index, line in enumerate(lines):
        heading = HEADING_RE.fullmatch(line)
        if heading and heading.group("version") == version:
            if start is not None:
                raise ReleaseNotesError(f"duplicate changelog section for {version}")
            start = index + 1
    if start is None:
        raise ReleaseNotesError(f"CHANGELOG.md has no exact section for {version}")

    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    body = "\n".join(lines[start:end]).strip()
    if not body:
        raise ReleaseNotesError(f"CHANGELOG.md section for {version} is empty")

    acceptances = []
    for line in lines[start:end]:
        if ACCEPTANCE_TOKEN not in line:
            continue
        acceptance = ACCEPTANCE_RE.fullmatch(line)
        if not acceptance:
            raise ReleaseNotesError(f"malformed security scan acceptance: {line}")
        vulnerability = acceptance.group("vulnerability")
        if not VULNERABILITY_RE.fullmatch(vulnerability):
            raise ReleaseNotesError(
                f"unsupported vulnerability identifier in scan acceptance: {vulnerability}"
            )
        acceptances.append(
            Acceptance(
                scanner="Trivy" if acceptance.group("scanner").startswith("Trivy") else "pip-audit",
                vulnerability=vulnerability,
                reason=acceptance.group("reason"),
                scope=acceptance.group("scope"),
            )
        )

    return ReleaseNotes(body=body + "\n", acceptances=tuple(acceptances))


def write_release_files(
    release_notes: ReleaseNotes,
    notes_output: Path,
    pip_audit_output: Path,
    trivy_amd64_output: Path,
    trivy_arm64_output: Path,
) -> None:
    notes_output.write_text(release_notes.body)
    pip_audit_output.write_text("\n".join(release_notes.pip_audit_ignores) + "\n")
    trivy_amd64_output.write_text("\n".join(release_notes.trivy_ignores("linux/amd64")) + "\n")
    trivy_arm64_output.write_text("\n".join(release_notes.trivy_ignores("linux/arm64")) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--notes-output", type=Path, default=Path("release-notes.md"))
    parser.add_argument("--pip-audit-output", type=Path, default=Path("pip-audit.ignore"))
    parser.add_argument("--trivy-amd64-output", type=Path, default=Path("trivy-amd64.ignore"))
    parser.add_argument("--trivy-arm64-output", type=Path, default=Path("trivy-arm64.ignore"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        release_notes = extract_release_notes(args.changelog.read_text(), args.tag)
        write_release_files(
            release_notes,
            args.notes_output,
            args.pip_audit_output,
            args.trivy_amd64_output,
            args.trivy_arm64_output,
        )
    except (OSError, ReleaseNotesError) as exc:
        raise SystemExit(f"release notes error: {exc}") from exc


if __name__ == "__main__":
    main()
