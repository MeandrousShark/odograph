#!/usr/bin/env bash
# Build a candidate public-release tree from this repository's committed
# state (HEAD), by explicit inclusion of only the paths a public snapshot
# should ever contain. Nothing here modifies this repository or touches
# any remote; it only writes files under the target directory you give it,
# and only ever prints the commit/push steps for a human to run by hand.
#
# Usage:
#   scripts/make_public_snapshot.sh <output-dir>
#   scripts/make_public_snapshot.sh --publish <clone-dir> <expected-origin>
#
# Bootstrap mode (the first form) requires <output-dir> to not already
# exist, or to be empty, since there is no prior tree to compare against.
#
# Publish mode (the second form) targets an existing clone of the public
# repository and regenerates its tree in place: it refuses an unsafe
# target, clears every top-level entry except .git, extracts the same
# allowlisted archive over the emptied tree, then reports the resulting
# `git status`/`git diff --stat` so a reviewer sees exactly what changed --
# including a file the private tree or the allowlist has since dropped.
# Extraction alone can only add or overwrite a file, never remove one that
# stopped shipping, so the clearing step is what a routine re-run needs to
# keep a dropped file from lingering in the published tree indefinitely.
# <expected-origin> must match the clone's configured `origin` remote
# exactly, so a mistyped path can't clear a directory that happens to be a
# git work tree but isn't the intended one.
#
# Either mode: the resulting tree is verified to exclude internal planning
# material, scanned for known private-infrastructure hostnames, and
# scanned for stray references (e.g. dangling links) to internal-only
# docs from within the files that do ship.
set -euo pipefail

if [ "$#" -ge 1 ] && [ "$1" = "--publish" ]; then
    MODE=publish
    shift
    if [ "$#" -ne 2 ]; then
        echo "usage: $0 --publish <clone-dir> <expected-origin>" >&2
        exit 1
    fi
    TARGET="$1"
    EXPECTED_ORIGIN="$2"
else
    MODE=bootstrap
    if [ "$#" -ne 1 ]; then
        echo "usage: $0 <output-dir>" >&2
        exit 1
    fi
    OUTDIR="$1"
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"

if [ "$MODE" = publish ]; then
    # Every refusal below runs before the clearing step further down: it is
    # the only thing standing between an unreviewed local edit and the `rm
    # -rf` that follows, so checking order here is a safety property, not
    # just validation ordering.
    if [ ! -d "$TARGET" ]; then
        echo "error: publish target '$TARGET' does not exist." >&2
        exit 1
    fi
    if ! TARGET_TOPLEVEL="$(git -C "$TARGET" rev-parse --show-toplevel 2>/dev/null)"; then
        echo "error: publish target '$TARGET' is not a git work tree." >&2
        exit 1
    fi
    TARGET_ABS="$(cd "$TARGET" && pwd)"
    if [ "$TARGET_TOPLEVEL" != "$TARGET_ABS" ]; then
        echo "error: publish target '$TARGET' is not the root of its git work tree (root is '$TARGET_TOPLEVEL')." >&2
        exit 1
    fi
    if [ -n "$(git -C "$TARGET_ABS" status --porcelain)" ]; then
        echo "error: publish target '$TARGET_ABS' has uncommitted changes; refusing to clear a dirty work tree." >&2
        exit 1
    fi
    TARGET_ORIGIN="$(git -C "$TARGET_ABS" remote get-url origin 2>/dev/null || true)"
    if [ -z "$TARGET_ORIGIN" ]; then
        echo "error: publish target '$TARGET_ABS' has no 'origin' remote." >&2
        exit 1
    fi
    if [ "$TARGET_ORIGIN" != "$EXPECTED_ORIGIN" ]; then
        echo "error: publish target's origin ('$TARGET_ORIGIN') does not match the expected origin ('$EXPECTED_ORIGIN')." >&2
        exit 1
    fi

    find "$TARGET_ABS" -mindepth 1 -maxdepth 1 ! -name '.git' -exec rm -rf -- {} +
    OUTDIR="$TARGET_ABS"
else
    if [ -e "$OUTDIR" ] && [ -n "$(ls -A "$OUTDIR" 2>/dev/null)" ]; then
        echo "error: '$OUTDIR' already exists and is not empty; refusing to overwrite it." >&2
        exit 1
    fi
    mkdir -p "$OUTDIR"
    OUTDIR="$(cd "$OUTDIR" && pwd)"
fi

cd "$REPO_ROOT"

# Explicit inclusion, with narrowly scoped pathspec exclusions for tracked
# private files: anything not listed here, or explicitly excluded below,
# never makes it into the archive, regardless of what exists in the working
# tree or git history. This is the actual privacy boundary -- everything
# else in this script is verification on top of it, not the boundary itself.
DIR_PATHS=(.github app tests static migrations scripts)
TOP_LEVEL_FILES=(
    README.md LICENSE SECURITY.md CONTRIBUTING.md THIRD_PARTY_NOTICES.md CHANGELOG.md
    compose.yaml compose.build.override.yml Dockerfile .dockerignore
    .env.example .gitignore
    requirements.txt requirements-dev.txt requirements-dev.lock requirements.lock
    # pyproject.toml only sets pytest's pythonpath; without it, `import
    # app` fails from inside the snapshot and its test suite can't run.
    pyproject.toml
)
DOCS_FILES=(
    docs/backups.md docs/configuration.md docs/install-compose.md docs/osrm.md
    docs/owntracks.md docs/privacy.md docs/releasing.md docs/reverse-proxy.md
    docs/security.md docs/upgrading.md docs/usage.md
    docs/images/usage-dashboard.png docs/images/usage-review.png
)
ARCHIVE_EXCLUDES=(
    ':!tests/test_handoff_contract.py'
)

ARCHIVE_PATHS=()
for path in "${DIR_PATHS[@]}" "${TOP_LEVEL_FILES[@]}" "${DOCS_FILES[@]}"; do
    if git cat-file -e "HEAD:$path" 2>/dev/null; then
        ARCHIVE_PATHS+=("$path")
    fi
done

if [ "${#ARCHIVE_PATHS[@]}" -eq 0 ]; then
    echo "error: none of the allowlisted paths exist at HEAD; nothing to archive." >&2
    exit 1
fi

git archive HEAD "${ARCHIVE_PATHS[@]}" "${ARCHIVE_EXCLUDES[@]}" | tar -x -C "$OUTDIR"

# Hard-verify the exclusions rather than trusting the allowlist above: a
# future edit to this script that accidentally widens DIR_PATHS (e.g. to
# "docs") must not silently ship internal planning material.
violations=0

if [ -d "$OUTDIR/docs" ]; then
    expected_docs=$'docs/backups.md\ndocs/configuration.md\ndocs/images/usage-dashboard.png\ndocs/images/usage-review.png\ndocs/install-compose.md\ndocs/osrm.md\ndocs/owntracks.md\ndocs/privacy.md\ndocs/releasing.md\ndocs/reverse-proxy.md\ndocs/security.md\ndocs/upgrading.md\ndocs/usage.md'
    actual_docs="$(find "$OUTDIR/docs" -type f | sed "s#^$OUTDIR/##" | sort)"
    if [ "$actual_docs" != "$expected_docs" ]; then
        echo "error: snapshot docs do not match the public allowlist." >&2
        echo "expected:" >&2
        printf '%s\n' "$expected_docs" >&2
        echo "actual:" >&2
        printf '%s\n' "$actual_docs" >&2
        violations=1
    fi
    while IFS= read -r -d '' f; do
        rel="${f#"$OUTDIR"/}"
        if [[ " ${DOCS_FILES[*]} " != *" $rel "* ]]; then
            echo "error: unexpected docs/ file in snapshot: $rel" >&2
            violations=1
        fi
    done < <(find "$OUTDIR/docs" -type f -print0)
else
    echo "error: snapshot is missing its public docs directory." >&2
    violations=1
fi

# .git is pruned from every find below, not just this one: a publish-mode
# target is an existing clone, so its .git holds thousands of packed
# objects that would otherwise slow every scan, wreck the file count, and
# could surface a "finding" inside a packed object with no corresponding
# shipped file to fix.
while IFS= read -r -d '' f; do
    echo "error: internal planning file leaked into snapshot: ${f#"$OUTDIR"/}" >&2
    violations=1
done < <(find "$OUTDIR" -name .git -prune -o \( -iname 'CLAUDE.md' -o -iname 'AGENTS.md' \) -print0)

while IFS= read -r -d '' f; do
    echo "error: .claude/ path leaked into snapshot: ${f#"$OUTDIR"/}" >&2
    violations=1
done < <(find "$OUTDIR" -name .git -prune -o -path '*/.claude/*' -o -name '.claude' -print0 2>/dev/null)

if [ "$violations" -ne 0 ]; then
    echo "error: exclusion checks failed; see above." >&2
    exit 1
fi

# Automated secret/private-infra scan, gating publication the same way a
# CI check would. gitleaks is preferred; the grep fallback only catches
# the specific hostnames known (from this project's own audit history) to
# have leaked into comments before, so it is deliberately not treated as
# equivalent coverage -- it exists so the script still fails loudly rather
# than passing silently when gitleaks isn't installed.
if command -v gitleaks >/dev/null 2>&1; then
    if ! gitleaks detect --no-git -s "$OUTDIR"; then
        echo "error: gitleaks found findings in the snapshot; see above." >&2
        exit 1
    fi
else
    echo "warning: gitleaks not found on PATH; falling back to a plain grep for known private hostnames. This is NOT equivalent to a real secrets scan -- install gitleaks before a real publication." >&2
    # Excludes this script's own filename: it necessarily contains these
    # hostnames as literal grep patterns, which would otherwise make the
    # fallback scan fail against itself every time. Also excludes .git,
    # for the same packed-object reasons noted above.
    # Matches the specific private-infrastructure hostnames, not the bare
    # "hannoncloud" domain: SECURITY.md deliberately publishes
    # security@hannoncloud.com as the security contact, and a bare-domain
    # pattern would flag that intentional, public-facing address every time
    # gitleaks isn't installed.
    if grep -RIn --exclude="$(basename "$0")" --exclude-dir=.git "sapporo\|miles\.hannoncloud\.com\|git\.hannoncloud\.com" "$OUTDIR"; then
        echo "error: found known private-infrastructure hostnames in the snapshot; see above." >&2
        exit 1
    fi
fi

# Always-on regardless of whether gitleaks ran: catches references TO
# internal-only docs from within shipped files (e.g. a stray README link
# to docs/HANDOFF.md), not just secrets or hostnames. This is a separate
# failure mode from the file-existence checks above -- a shipped file can
# legitimately not BE an internal doc while still pointing at one, and
# that link would dangle in the public repo since the target never ships.
# Also catches bare internal milestone markers like "(M6)", bare
# work-item markers like "W6", and the bare filenames (HANDOFF.md,
# DESIGN.md, PHASE3.md, PUBLIC.md, PUBLIC-M1.md, ...) without a docs/
# prefix -- a real audit pass found these forms leaking into migration
# comments, template comments, and even user-facing settings.html copy,
# including at least one bare filename a docs/-prefixed pattern alone
# would have missed. Milestone numbers are only matched in parenthesized
# form "(M6)" rather than bare "\bM[0-9]+\b": bare M-numbers collide with
# legitimate content (e.g. Apple Silicon chip names, unit abbreviations),
# whereas bare work-item markers like "W6" have no such collision risk.
INTERNAL_DOC_PATTERN="docs/DESIGN|docs/HANDOFF|docs/PHASE|docs/PUBLIC|CLAUDE\.md|AGENTS\.md|\(M[0-9]+\)|\bW[0-9]+\b|HANDOFF\.md|DESIGN\.md|PHASE[0-9]|PUBLIC\.md|PUBLIC-M"
while IFS= read -r -d '' f; do
    if grep -InE "$INTERNAL_DOC_PATTERN" "$f" >/dev/null 2>&1; then
        echo "error: internal-doc reference found in shipped file: ${f#"$OUTDIR"/}" >&2
        grep -InE "$INTERNAL_DOC_PATTERN" "$f" >&2
        violations=1
    fi
done < <(find "$OUTDIR" -name .git -prune -o -type f ! -name "$(basename "$0")" -print0)

if [ "$violations" -ne 0 ]; then
    echo "error: internal-doc reference checks failed; see above." >&2
    exit 1
fi

file_count="$(find "$OUTDIR" -name .git -prune -o -type f -print | wc -l | tr -d ' ')"
echo "Snapshot built at: $OUTDIR"
echo "File count: $file_count"
echo

if [ "$MODE" = publish ]; then
    echo "Effect on '$OUTDIR':"
    git -C "$OUTDIR" status --short
    echo
    git -C "$OUTDIR" diff --stat HEAD
    echo
    echo "Next steps (not performed by this script):"
    echo "  cd '$OUTDIR'"
    echo "  git add -A"
    echo "  git commit -m 'Describe this public change'"
    echo "  git push"
else
    echo "Next steps (not performed by this script):"
    echo "  cd '$OUTDIR'"
    echo "  git init"
    echo "  git add -A"
    echo "  git commit -m 'Initial public release (vX.Y.Z)'"
    echo "  # Push to a throwaway PRIVATE GitHub repo first and review its file"
    echo "  # listing before ever pushing to a public remote:"
    echo "  git remote add origin <private-rehearsal-repo-url>"
    echo "  git push -u origin HEAD"
fi
