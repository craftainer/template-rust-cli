#!/usr/bin/env bash
# The guard of release-binaries.yml: a dispatch may only release a real
# release tag, and only from the default branch's workflow definition (a
# dispatch from another branch would run unreviewed workflow code with the
# release permissions and secrets).
#
# Reads: RELEASE_TAG (the `tag` input), GITHUB_REF and DEFAULT_BRANCH.
# Prints `version=` and `prerelease=` lines (for $GITHUB_OUTPUT) on success.
set -euo pipefail

: "${RELEASE_TAG:?RELEASE_TAG is required}"
: "${GITHUB_REF:?GITHUB_REF is required}"
: "${DEFAULT_BRANCH:?DEFAULT_BRANCH is required}"

if [[ $GITHUB_REF != "refs/heads/$DEFAULT_BRANCH" ]]; then
    echo "::error::run this workflow from the default branch ($DEFAULT_BRANCH), not '$GITHUB_REF'" >&2
    exit 1
fi

# Same shape .github/scripts/compute_next_version.py produces: vX.Y.Z or
# vX.Y.Z-<prerelease>.
if ! [[ $RELEASE_TAG =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z][0-9A-Za-z.-]*)?$ ]]; then
    echo "::error::not a release tag: '$RELEASE_TAG' (expected vX.Y.Z or vX.Y.Z-<prerelease>)" >&2
    exit 1
fi

version=${RELEASE_TAG#v}
echo "version=$version"
if [[ $version == *-* ]]; then
    echo "prerelease=true"
else
    echo "prerelease=false"
fi
