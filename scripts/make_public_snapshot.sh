#!/usr/bin/env bash
# Build a candidate public-release tree from this repository's committed
# state (HEAD), by explicit inclusion of only the paths a public snapshot
# should ever contain. Nothing here modifies this repository or touches
# any remote; it only writes files under the output directory you give it.
#
# Usage:
#   scripts/make_public_snapshot.sh <output-dir>
#
# <output-dir> must not already exist, or must be empty. On success it
# contains the candidate public tree, verified to exclude internal
# planning material, scanned for known private-infrastructure hostnames,
# and scanned for stray references (e.g. dangling links) to internal-only
# docs from within the files that do ship.
# The script prints next steps (init a fresh repo, one commit, push to a
# private rehearsal remote) rather than performing them, so a human
# reviews the result before anything leaves this machine.
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <output-dir>" >&2
    exit 1
fi

OUTDIR="$1"
REPO_ROOT="$(git rev-parse --show-toplevel)"

if [ -e "$OUTDIR" ] && [ -n "$(ls -A "$OUTDIR" 2>/dev/null)" ]; then
    echo "error: '$OUTDIR' already exists and is not empty; refusing to overwrite it." >&2
    exit 1
fi
mkdir -p "$OUTDIR"
OUTDIR="$(cd "$OUTDIR" && pwd)"

cd "$REPO_ROOT"

# Explicit inclusion, not exclusion: anything not listed here never makes
# it into the archive, regardless of what exists in the working tree or
# git history. This is the actual privacy boundary -- everything else in
# this script is verification on top of it, not the boundary itself.
DIR_PATHS=(.github app tests static migrations scripts)
TOP_LEVEL_FILES=(
    README.md LICENSE SECURITY.md CONTRIBUTING.md THIRD_PARTY_NOTICES.md CHANGELOG.md
    compose.yaml compose.build.override.yml Dockerfile .dockerignore
    .env.example .gitignore
    requirements.txt requirements-dev.txt requirements.lock
    # pyproject.toml only sets pytest's pythonpath; without it, `import
    # app` fails from inside the snapshot and its test suite can't run.
    pyproject.toml
)
DOCS_FILES=(
    docs/backups.md docs/osrm.md docs/owntracks.md docs/privacy.md
    docs/releasing.md docs/reverse-proxy.md docs/security.md docs/upgrading.md
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

git archive HEAD "${ARCHIVE_PATHS[@]}" | tar -x -C "$OUTDIR"

# Hard-verify the exclusions rather than trusting the allowlist above: a
# future edit to this script that accidentally widens DIR_PATHS (e.g. to
# "docs") must not silently ship internal planning material.
violations=0

if [ -d "$OUTDIR/docs" ]; then
    expected_docs=$'docs/backups.md\ndocs/osrm.md\ndocs/owntracks.md\ndocs/privacy.md\ndocs/releasing.md\ndocs/reverse-proxy.md\ndocs/security.md\ndocs/upgrading.md'
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

while IFS= read -r -d '' f; do
    echo "error: internal planning file leaked into snapshot: ${f#"$OUTDIR"/}" >&2
    violations=1
done < <(find "$OUTDIR" \( -iname 'CLAUDE.md' -o -iname 'AGENTS.md' \) -print0)

while IFS= read -r -d '' f; do
    echo "error: .claude/ path leaked into snapshot: ${f#"$OUTDIR"/}" >&2
    violations=1
done < <(find "$OUTDIR" -path '*/.claude/*' -o -name '.claude' -print0 2>/dev/null)

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
    # fallback scan fail against itself every time.
    # Matches the specific private-infrastructure hostnames, not the bare
    # "hannoncloud" domain: SECURITY.md deliberately publishes
    # security@hannoncloud.com as the security contact, and a bare-domain
    # pattern would flag that intentional, public-facing address every time
    # gitleaks isn't installed.
    if grep -RIn --exclude="$(basename "$0")" "sapporo\|miles\.hannoncloud\.com\|git\.hannoncloud\.com" "$OUTDIR"; then
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
# DESIGN.md, PHASE3-M3.md, PUBLIC.md, PUBLIC-M1.md, ...) without a docs/
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
done < <(find "$OUTDIR" -type f ! -name "$(basename "$0")" -print0)

if [ "$violations" -ne 0 ]; then
    echo "error: internal-doc reference checks failed; see above." >&2
    exit 1
fi

file_count="$(find "$OUTDIR" -type f | wc -l | tr -d ' ')"
echo "Snapshot built at: $OUTDIR"
echo "File count: $file_count"
echo
echo "Next steps (not performed by this script):"
echo "  cd '$OUTDIR'"
echo "  git init"
echo "  git add -A"
echo "  git commit -m 'Initial public release (vX.Y.Z)'"
echo "  # Push to a throwaway PRIVATE GitHub repo first and review its file"
echo "  # listing before ever pushing to a public remote:"
echo "  git remote add origin <private-rehearsal-repo-url>"
echo "  git push -u origin HEAD"
