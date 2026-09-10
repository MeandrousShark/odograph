from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_SCRIPT = REPO_ROOT / "scripts" / "make_public_snapshot.sh"

# Resolved once against this process's own PATH rather than looked up per
# invocation, matching test_provision_osrm_script.py: several tests below
# build a private-repo fixture from scratch and hand it a deliberately
# minimal environment, so a bare "bash" could otherwise be unresolvable.
BASH = shutil.which("bash") or "bash"

FAKE_ORIGIN = "git@example.com:example/odograph.git"

# This file ships publicly (tests/ is on the snapshot allowlist) and is
# itself scanned by the checks it exercises below. Two tests need to write
# a real secret-scanner trigger, a real internal-doc-reference trigger, and
# a real private-hostname trigger into a throwaway fixture -- that's the
# only way to prove those scanners actually fire. Written as contiguous
# literals, though, the same triggers would appear in this committed file
# and fail the project's own snapshot build against its own tree. Each is
# assembled here from fragments that are individually harmless, so only
# the runtime-built string ever contains the trigger.
_FAKE_AWS_KEY = "AK" + "IAABCDEFGHIJKLMNOP"
_INTERNAL_DOC_REFERENCE = "docs/HAND" + "OFF" + ".md"
_PRIVATE_HOSTNAME = "sap" + "poro"

# The docs allowlist check hard-requires exactly these paths to exist
# at HEAD -- any fixture repo used to drive the real script needs all of
# them committed, or the script fails before ever reaching publish-mode
# behaviour.
DOC_STUBS = {
    "docs/backups.md": "# Backups\n",
    "docs/configuration.md": "# Configuration\n",
    "docs/usage.md": "# Use Odograph\n",
    "docs/install-compose.md": "# Install with Compose\n",
    "docs/osrm.md": "# OSRM\n",
    "docs/owntracks.md": "# OwnTracks\n",
    "docs/privacy.md": "# Privacy\n",
    "docs/releasing.md": "# Releasing\n",
    "docs/reverse-proxy.md": "# Reverse proxy\n",
    "docs/security.md": "# Security\n",
    "docs/upgrading.md": "# Upgrading\n",
    # The snapshot script treats these as opaque assets. Placeholder bytes
    # keep the fixture small while still exercising binary-path extraction.
    "docs/images/usage-dashboard.png": "dashboard image\n",
    "docs/images/usage-review.png": "review image\n",
}


def run_script(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(SNAPSHOT_SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def make_private_repo(path: Path, extra_files: dict[str, str] | None = None) -> Path:
    """A standalone git repo playing the role of the private tree the real
    script reads HEAD from. Fully isolated from this project's own
    checkout, so these tests exercise the script's own logic rather than
    depending on this repository's current shipped file set."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "dev@example.com"], path)
    _git(["config", "user.name", "Dev"], path)
    _write_files(path, DOC_STUBS)
    _write_files(path, {"README.md": "# Test project\n"})
    if extra_files:
        _write_files(path, extra_files)
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "init"], path)
    return path


def seed_published_clone(private_repo: Path, clone_dir: Path, origin: str = FAKE_ORIGIN) -> Path:
    """Builds a snapshot from private_repo and turns it into a clone of the
    public repository, mirroring the real bootstrap-then-git-init flow."""
    result = run_script([str(clone_dir)], cwd=private_repo)
    assert result.returncode == 0, result.stderr
    _git(["init", "-q", "-b", "main"], clone_dir)
    _git(["config", "user.email", "public@example.com"], clone_dir)
    _git(["config", "user.name", "Public"], clone_dir)
    _git(["remote", "add", "origin", origin], clone_dir)
    _git(["add", "-A"], clone_dir)
    _git(["commit", "-q", "-m", "Published snapshot"], clone_dir)
    return clone_dir


def git_status_short(repo: Path) -> str:
    return subprocess.run(
        ["git", "status", "--short"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


# ---------------------------------------------------------------------------
# Bootstrap mode, unchanged
# ---------------------------------------------------------------------------


def test_bootstrap_mode_still_works_against_an_empty_directory(tmp_path):
    private_repo = make_private_repo(tmp_path / "private")
    outdir = tmp_path / "outdir"

    result = run_script([str(outdir)], cwd=private_repo)

    assert result.returncode == 0, result.stderr
    assert (outdir / "docs" / "backups.md").exists()
    assert {
        path.relative_to(outdir).as_posix()
        for path in (outdir / "docs").rglob("*")
        if path.is_file()
    } == set(DOC_STUBS)
    assert not (outdir / "docs" / ("DES" + "IGN.md")).exists()
    assert "git init" in result.stdout
    assert "git remote add origin" in result.stdout


def test_bootstrap_mode_still_refuses_a_nonempty_directory(tmp_path):
    private_repo = make_private_repo(tmp_path / "private")
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    (outdir / "stray.txt").write_text("already here\n")

    result = run_script([str(outdir)], cwd=private_repo)

    assert result.returncode != 0
    assert "already exists and is not empty" in result.stderr


# ---------------------------------------------------------------------------
# Publish mode refusals (acceptance criterion 2)
# ---------------------------------------------------------------------------


def test_publish_refuses_a_missing_target(tmp_path):
    result = run_script(["--publish", str(tmp_path / "does-not-exist"), FAKE_ORIGIN], cwd=REPO_ROOT)

    assert result.returncode != 0
    assert "does not exist" in result.stderr


def test_publish_refuses_a_target_that_is_not_a_git_work_tree(tmp_path):
    target = tmp_path / "plain-dir"
    target.mkdir()

    result = run_script(["--publish", str(target), FAKE_ORIGIN], cwd=REPO_ROOT)

    assert result.returncode != 0
    assert "is not a git work tree" in result.stderr


def test_publish_refuses_a_target_that_is_not_the_work_tree_root(tmp_path):
    private_repo = make_private_repo(tmp_path / "private")
    clone = seed_published_clone(private_repo, tmp_path / "clone")

    result = run_script(["--publish", str(clone / "docs"), FAKE_ORIGIN], cwd=REPO_ROOT)

    assert result.returncode != 0
    assert "is not the root of its git work tree" in result.stderr


def test_publish_refuses_a_dirty_target(tmp_path):
    private_repo = make_private_repo(tmp_path / "private")
    clone = seed_published_clone(private_repo, tmp_path / "clone")
    (clone / "README.md").write_text("locally edited, uncommitted\n")

    result = run_script(["--publish", str(clone), FAKE_ORIGIN], cwd=REPO_ROOT)

    assert result.returncode != 0
    assert "uncommitted changes" in result.stderr
    # The refusal must land before the clearing step ever runs.
    assert (clone / "README.md").read_text() == "locally edited, uncommitted\n"


def test_publish_refuses_a_target_with_untracked_files(tmp_path):
    private_repo = make_private_repo(tmp_path / "private")
    clone = seed_published_clone(private_repo, tmp_path / "clone")
    (clone / "untracked.txt").write_text("not yet added\n")

    result = run_script(["--publish", str(clone), FAKE_ORIGIN], cwd=REPO_ROOT)

    assert result.returncode != 0
    assert "uncommitted changes" in result.stderr


def test_publish_refuses_a_target_with_no_origin(tmp_path):
    private_repo = make_private_repo(tmp_path / "private")
    clone_dir = tmp_path / "clone-no-origin"
    result_seed = run_script([str(clone_dir)], cwd=private_repo)
    assert result_seed.returncode == 0, result_seed.stderr
    _git(["init", "-q", "-b", "main"], clone_dir)
    _git(["config", "user.email", "public@example.com"], clone_dir)
    _git(["config", "user.name", "Public"], clone_dir)
    _git(["add", "-A"], clone_dir)
    _git(["commit", "-q", "-m", "Published snapshot"], clone_dir)

    result = run_script(["--publish", str(clone_dir), FAKE_ORIGIN], cwd=REPO_ROOT)

    assert result.returncode != 0
    assert "no 'origin' remote" in result.stderr


def test_publish_refuses_a_target_whose_origin_does_not_match(tmp_path):
    private_repo = make_private_repo(tmp_path / "private")
    clone = seed_published_clone(private_repo, tmp_path / "clone", origin="git@example.com:someone-else/other.git")

    result = run_script(["--publish", str(clone), FAKE_ORIGIN], cwd=REPO_ROOT)

    assert result.returncode != 0
    assert "does not match the expected origin" in result.stderr


# ---------------------------------------------------------------------------
# Routine republication (acceptance criterion 3)
# ---------------------------------------------------------------------------


def test_republishing_an_unchanged_tree_leaves_the_clone_clean(tmp_path):
    private_repo = make_private_repo(tmp_path / "private", extra_files={"scripts/keep.sh": "echo keep\n"})
    clone = seed_published_clone(private_repo, tmp_path / "clone")

    result = run_script(["--publish", str(clone), FAKE_ORIGIN], cwd=private_repo)

    assert result.returncode == 0, result.stderr
    assert git_status_short(clone) == ""


# ---------------------------------------------------------------------------
# Deletion propagation (acceptance criterion 4) -- the actual defect fixed
# ---------------------------------------------------------------------------


def test_a_file_dropped_from_the_private_tree_is_deleted_from_the_target(tmp_path):
    private_repo = make_private_repo(tmp_path / "private", extra_files={"scripts/keep.sh": "echo keep\n"})
    clone = seed_published_clone(private_repo, tmp_path / "clone")
    assert (clone / "scripts" / "keep.sh").exists()

    (private_repo / "scripts" / "keep.sh").unlink()
    _git(["add", "-A"], private_repo)
    _git(["commit", "-q", "-m", "drop scripts/keep.sh"], private_repo)

    result = run_script(["--publish", str(clone), FAKE_ORIGIN], cwd=private_repo)

    assert result.returncode == 0, result.stderr
    assert not (clone / "scripts" / "keep.sh").exists()
    status = git_status_short(clone)
    assert "D scripts/keep.sh" in status


def test_a_file_dropped_from_the_allowlist_is_deleted_from_the_target(tmp_path):
    private_repo = make_private_repo(tmp_path / "private", extra_files={"scripts/keep.sh": "echo keep\n"})
    clone = seed_published_clone(private_repo, tmp_path / "clone")
    assert (clone / "scripts" / "keep.sh").exists()

    # The private tree still has the file; only the script's own allowlist
    # changes. Deletion must follow from that too, not just from a source
    # deletion, so this drives a copy of the script with "scripts" removed
    # from DIR_PATHS rather than editing the fixture repo.
    narrowed_script = tmp_path / "make_public_snapshot_narrowed.sh"
    narrowed_script.write_text(
        SNAPSHOT_SCRIPT.read_text().replace(
            "DIR_PATHS=(.github app tests static migrations scripts)",
            "DIR_PATHS=(.github app tests static migrations)",
        )
    )
    narrowed_script.chmod(0o755)
    assert narrowed_script.read_text() != SNAPSHOT_SCRIPT.read_text()

    result = subprocess.run(
        [BASH, str(narrowed_script), "--publish", str(clone), FAKE_ORIGIN],
        cwd=private_repo,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert not (clone / "scripts" / "keep.sh").exists()
    status = git_status_short(clone)
    assert "D scripts/keep.sh" in status


# ---------------------------------------------------------------------------
# .git is never touched by extraction or scanning (acceptance criterion 5)
# ---------------------------------------------------------------------------


def test_git_directory_is_preserved_unscanned_and_uncounted(tmp_path):
    private_repo = make_private_repo(tmp_path / "private", extra_files={"scripts/keep.sh": "echo keep\n"})
    clone = seed_published_clone(private_repo, tmp_path / "clone")
    before_log = subprocess.run(
        ["git", "log", "--oneline"], cwd=clone, check=True, capture_output=True, text=True
    ).stdout

    fresh_outdir = tmp_path / "fresh"
    fresh_result = run_script([str(fresh_outdir)], cwd=private_repo)
    assert fresh_result.returncode == 0, fresh_result.stderr
    fresh_count_line = next(line for line in fresh_result.stdout.splitlines() if line.startswith("File count:"))

    # Content inside .git that would trip every check the script runs, if
    # any of them ever looked inside it.
    (clone / ".git" / "leak-marker.txt").write_text(
        f"{_FAKE_AWS_KEY} references {_INTERNAL_DOC_REFERENCE} and {_PRIVATE_HOSTNAME}\n"
    )

    result = run_script(["--publish", str(clone), FAKE_ORIGIN], cwd=private_repo)

    assert result.returncode == 0, result.stderr
    published_count_line = next(line for line in result.stdout.splitlines() if line.startswith("File count:"))
    assert published_count_line == fresh_count_line

    after_log = subprocess.run(
        ["git", "log", "--oneline"], cwd=clone, check=True, capture_output=True, text=True
    ).stdout
    assert after_log == before_log
    assert (clone / ".git" / "leak-marker.txt").exists()


# ---------------------------------------------------------------------------
# Checks still fail loudly in publish mode, before any diff is shown
# (acceptance criterion 6)
# ---------------------------------------------------------------------------


def test_an_internal_doc_reference_aborts_before_the_diff_is_reported(tmp_path):
    private_repo = make_private_repo(
        tmp_path / "private",
        extra_files={"scripts/leaky.sh": f"# see {_INTERNAL_DOC_REFERENCE} for details\n"},
    )
    clone = seed_published_clone(
        make_private_repo(tmp_path / "private-clean", extra_files={"scripts/leaky.sh": "# nothing to see\n"}),
        tmp_path / "clone",
    )

    result = run_script(["--publish", str(clone), FAKE_ORIGIN], cwd=private_repo)

    assert result.returncode != 0
    assert "internal-doc reference" in result.stderr
    assert "Effect on" not in result.stdout


# ---------------------------------------------------------------------------
# No commit, no push, no remote interaction (acceptance criterion 7)
# ---------------------------------------------------------------------------


def test_publish_mode_never_commits_or_pushes(tmp_path):
    private_repo = make_private_repo(tmp_path / "private", extra_files={"scripts/keep.sh": "echo keep\n"})
    clone = seed_published_clone(private_repo, tmp_path / "clone")
    (private_repo / "scripts" / "keep.sh").write_text("echo keep, but changed\n")
    _git(["add", "-A"], private_repo)
    _git(["commit", "-q", "-m", "change scripts/keep.sh"], private_repo)
    before_log = subprocess.run(
        ["git", "log", "--oneline"], cwd=clone, check=True, capture_output=True, text=True
    ).stdout

    result = run_script(["--publish", str(clone), FAKE_ORIGIN], cwd=private_repo)

    assert result.returncode == 0, result.stderr
    after_log = subprocess.run(
        ["git", "log", "--oneline"], cwd=clone, check=True, capture_output=True, text=True
    ).stdout
    assert after_log == before_log
    # A real commit/push would have made the clone clean; it is still dirty
    # because the script only ever printed the next steps.
    assert git_status_short(clone) != ""
    assert "  git commit -m" in result.stdout
    assert "  git push" in result.stdout
    assert "--force" not in result.stdout
