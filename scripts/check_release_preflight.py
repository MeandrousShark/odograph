#!/usr/bin/env python3
"""Validate release preflight inputs and remote version state."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Mapping

from check_release_contract import _app_image, validate_release_contract
from release_notes import extract_release_notes, write_release_files


class PreflightError(ValueError):
    pass


REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def image_for_repository(repository: str) -> str:
    if not REPOSITORY_RE.fullmatch(repository):
        raise PreflightError(f"invalid GitHub repository name: {repository!r}")
    return f"ghcr.io/{repository.lower()}"


def derive_version(compose_text: str, repository: str) -> str:
    image = image_for_repository(repository)
    app_image = _app_image(compose_text)
    prefix = f"{image}:"
    if not app_image.startswith(prefix):
        raise PreflightError(
            f"compose app image is {app_image!r}, expected a tag on {image!r}"
        )
    version = app_image.removeprefix(prefix)
    if not version or "@" in version or "/" in version:
        raise PreflightError(f"compose app image has an invalid release tag: {app_image!r}")
    return version


def _http_request(
    url: str, headers: Mapping[str, str] | None = None
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()
    except urllib.error.URLError as exc:
        raise PreflightError(f"request failed for {url}: {exc.reason}") from exc


def _github_resource(
    repository: str, path: str, token: str
) -> tuple[int, bytes]:
    quoted_repository = "/".join(
        urllib.parse.quote(part, safe="") for part in repository.split("/")
    )
    status, _headers, body = _http_request(
        f"https://api.github.com/repos/{quoted_repository}/{path}",
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "odograph-release-preflight",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if status not in {200, 404}:
        raise PreflightError(
            f"GitHub returned HTTP {status} while checking {path}; refusing to guess"
        )
    return status, body


def _bearer_parameters(challenge: str) -> dict[str, str]:
    if not challenge.lower().startswith("bearer "):
        raise PreflightError("GHCR returned an unsupported authentication challenge")
    parameters = {
        key.lower(): value
        for key, value in re.findall(r'([A-Za-z]+)="([^"]*)"', challenge[7:])
    }
    if not parameters.get("realm") or not parameters.get("service"):
        raise PreflightError("GHCR authentication challenge is incomplete")
    return parameters


def _registry_manifest_status(repository: str, version: str) -> int:
    image_path = repository.lower()
    manifest_url = (
        f"https://ghcr.io/v2/{image_path}/manifests/"
        f"{urllib.parse.quote(version, safe='')}"
    )
    status, headers, _body = _http_request(
        manifest_url, {"Accept": MANIFEST_ACCEPT, "User-Agent": "odograph-release-preflight"}
    )
    if status == 401:
        parameters = _bearer_parameters(headers.get("WWW-Authenticate", ""))
        query = urllib.parse.urlencode(
            {
                "service": parameters["service"],
                "scope": parameters.get("scope", f"repository:{image_path}:pull"),
            }
        )
        token_status, _token_headers, token_body = _http_request(
            f"{parameters['realm']}?{query}",
            {"User-Agent": "odograph-release-preflight"},
        )
        if token_status != 200:
            raise PreflightError(
                f"GHCR token service returned HTTP {token_status}; refusing to guess"
            )
        try:
            token_payload = json.loads(token_body)
            registry_token = token_payload.get("token") or token_payload["access_token"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PreflightError("GHCR token service returned an invalid response") from exc
        status, _headers, _body = _http_request(
            manifest_url,
            {
                "Accept": MANIFEST_ACCEPT,
                "Authorization": f"Bearer {registry_token}",
                "User-Agent": "odograph-release-preflight",
            },
        )
    if status not in {200, 404}:
        raise PreflightError(
            f"GHCR returned HTTP {status} while checking {repository}:{version}; "
            "refusing to guess"
        )
    return status


def check_remote_version_state(
    repository: str,
    version: str,
    expected_revision: str,
    mode: str,
    token: str,
) -> dict[str, int | str]:
    if mode == "skip":
        return {"mode": mode}
    if not token:
        raise PreflightError("GITHUB_TOKEN is required for remote version checks")

    quoted_version = urllib.parse.quote(version, safe="")
    tag_status, _tag_body = _github_resource(
        repository, f"git/ref/tags/{quoted_version}", token
    )
    release_status, release_body = _github_resource(
        repository, f"releases/tags/{quoted_version}", token
    )
    manifest_status = _registry_manifest_status(repository, version)
    statuses = {
        "git_tag": tag_status,
        "github_release": release_status,
        "ghcr_manifest": manifest_status,
    }

    expected_status = 404 if mode == "unused" else 200
    mismatches = [name for name, status in statuses.items() if status != expected_status]
    if mismatches:
        expected = "absent (HTTP 404)" if mode == "unused" else "present (HTTP 200)"
        detail = ", ".join(f"{name}=HTTP {statuses[name]}" for name in statuses)
        raise PreflightError(
            f"release version state is not uniformly {expected}: {detail}"
        )

    result: dict[str, int | str] = {"mode": mode, **statuses}
    if mode == "published":
        commit_status, commit_body = _github_resource(
            repository, f"commits/{quoted_version}", token
        )
        if commit_status != 200:
            raise PreflightError(f"published tag {version} does not resolve to a commit")
        try:
            tag_revision = json.loads(commit_body)["sha"]
            release_tag = json.loads(release_body)["tag_name"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PreflightError("published GitHub metadata is invalid") from exc
        if tag_revision != expected_revision:
            raise PreflightError(
                f"published tag resolves to {tag_revision}, expected {expected_revision}"
            )
        if release_tag != version:
            raise PreflightError(
                f"GitHub release reports tag {release_tag!r}, expected {version!r}"
            )
        result["tag_revision"] = tag_revision
    return result


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError("could not resolve the checked-out revision") from exc
    return result.stdout.strip()


def _validate(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    image = image_for_repository(args.repository)
    version = args.version or derive_version(
        (root / "compose.yaml").read_text(), args.repository
    )
    revision = _git_head(root)
    if revision != args.expected_revision:
        raise PreflightError(
            f"checked out revision is {revision}, expected {args.expected_revision}"
        )

    validate_release_contract(root, version, image)
    release_notes = extract_release_notes((root / "CHANGELOG.md").read_text(), version)
    if args.require_empty_acceptances and release_notes.acceptances:
        raise PreflightError(
            f"{version} has security scan acceptances, but this preflight requires none"
        )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    write_release_files(
        release_notes,
        output_dir / "release-notes.md",
        output_dir / "pip-audit.ignore",
        output_dir / "trivy-amd64.ignore",
        output_dir / "trivy-arm64.ignore",
    )
    remote = check_remote_version_state(
        args.repository,
        version,
        revision,
        args.uniqueness,
        os.environ.get("GITHUB_TOKEN", ""),
    )
    evidence = {
        "image": image,
        "revision": revision,
        "version": version,
        "remote_version_state": remote,
    }
    (output_dir / "release-preflight.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    )
    if args.github_output:
        with args.github_output.open("a") as output:
            output.write(f"image={image}\nrevision={revision}\nversion={version}\n")
    print(f"Release preflight inputs valid for {image}:{version} at {revision}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    version = subparsers.add_parser("version", help="read the Compose app version")
    version.add_argument("--repository", required=True)
    version.add_argument("--compose", type=Path, default=Path("compose.yaml"))

    validate = subparsers.add_parser("validate", help="validate a preflight checkout")
    validate.add_argument("--root", type=Path, default=Path("."))
    validate.add_argument("--repository", required=True)
    validate.add_argument("--expected-revision", required=True)
    validate.add_argument("--version")
    validate.add_argument(
        "--uniqueness", choices=("skip", "unused", "published"), required=True
    )
    validate.add_argument("--require-empty-acceptances", action="store_true")
    validate.add_argument("--output-dir", type=Path, required=True)
    validate.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        if args.command == "version":
            print(derive_version(args.compose.read_text(), args.repository))
        else:
            _validate(args)
    except (OSError, PreflightError, ValueError) as exc:
        raise SystemExit(f"release preflight error: {exc}") from exc


if __name__ == "__main__":
    main()
