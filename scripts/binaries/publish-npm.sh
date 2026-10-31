#!/usr/bin/env bash
# Publishes the npm tarballs of one release: every platform package first, the
# root package last (its optionalDependencies must resolve the moment it is
# visible), each once -- exactly the files that were attested, never rebuilt.
#
# Usage: publish-npm.sh <platform-tarball-dir> <root-tarball-dir>
# Reads: NPM_TOKEN (empty = absent), GITHUB_REPOSITORY (each package.json's
# repository.url must name it; npm rejects provenance otherwise).
#   - NPM_TOKEN set (the family's staged first publish, before the packages
#     have trusted publishers): `npm stage publish` with that stage-only token.
#     Nothing is public until a maintainer approves each printed stage id
#     with 2FA, for all seven packages.
#   - NPM_TOKEN empty: `npm publish --provenance` through OIDC trusted
#     publishing (the job's `id-token: write`), no stored credential.
# The dist-tag is `latest` for a stable version, the prerelease's first
# identifier (else `next`) for a prerelease, and `v<major>-latest` for a stable
# version older than the registry's `latest` (a backport).
# `npm_config_dry_run=true` makes either path a dry run.
set -euo pipefail

platform_dir=${1:?usage: publish-npm.sh <platform-tarball-dir> <root-tarball-dir>}
root_dir=${2:?usage: publish-npm.sh <platform-tarball-dir> <root-tarball-dir>}

# npm's OIDC trusted publishing breaks if this is set, even empty.
unset NODE_AUTH_TOKEN

# Staged publishing needs npm >= 11.15.0 and OIDC trusted publishing >= 11.5.1,
# and the Node on the runner may ship an older npm: run this exact one (the
# same pin as template-npm-package), installed into a temporary prefix.
NPM_VERSION=${NPM_VERSION:-11.19.1}
export npm_config_update_notifier=false
NPM_BIN=$(type -P npm)
if [[ $("$NPM_BIN" --version) != "$NPM_VERSION" ]]; then
    prefix=$(mktemp -d)
    trap 'rm -rf "$prefix"' EXIT
    "$NPM_BIN" install --prefix "$prefix" --no-audit --no-fund --silent "npm@$NPM_VERSION"
    NPM_BIN=$prefix/node_modules/.bin/npm
fi
npm() { "$NPM_BIN" "$@"; }

tarballs=()
for dir in "$platform_dir" "$root_dir"; do
    found=("$dir"/*.tgz)
    if [[ ! -e ${found[0]} ]]; then
        echo "no tarballs in $dir" >&2
        exit 1
    fi
    tarballs+=("${found[@]}")
done

tarball_field() { # <tarball> <field>
    tar -xzOf "$1" package/package.json | python3 -c \
        'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$2"
}

tarball_repository() { # <tarball> -> repository.url
    tar -xzOf "$1" package/package.json | python3 -c \
        'import json, sys; print(json.load(sys.stdin)["repository"]["url"])'
}

# <package spec> <field> -> the field of `npm view`, nothing when the package
# or version is unknown (E404); any other failure stops the release.
npm_view() {
    local out
    if out=$(npm view --json "$1" "$2" 2>/dev/null); then
        printf '%s' "$out" | python3 -c '
import json, sys
text = sys.stdin.read().strip()
if text:
    value = json.loads(text)
    print(value[-1] if isinstance(value, list) else value)'
        return
    fi
    if printf '%s' "$out" | python3 -c '
import json, sys
try:
    sys.exit(0 if json.load(sys.stdin)["error"]["code"] == "E404" else 1)
except Exception:
    sys.exit(1)'; then
        return 0
    fi
    echo "npm view $1 $2 failed:" >&2
    npm view "$1" "$2" >&2 || true
    return 1
}

dist_tag() { # <name> <version>
    local name=$1 version=${2%%+*} pre id current
    if [[ $version == *-* ]]; then
        pre=${version#*-}
        id=${pre%%.*}
        if [[ -z $id || $id =~ ^[0-9]+$ ]]; then echo next; else echo "$id"; fi
        return
    fi
    current=$(npm_view "$name" dist-tags.latest) || return 1
    current=${current%%-*}
    if [[ -z $current || $(printf '%s\n%s\n' "$current" "$version" | sort -V | tail -1) == "$version" ]]; then
        echo latest
    else
        echo "v${version%%.*}-latest"
    fi
}

# Everything that can be checked before the first upload is checked first, so a
# problem with the last package does not leave the release half published.
for tarball in "${tarballs[@]}"; do
    if [[ -n ${GITHUB_REPOSITORY:-} ]]; then
        want="git+https://github.com/${GITHUB_REPOSITORY}.git"
        have=$(tarball_repository "$tarball")
        if [[ $have != "$want" ]]; then
            echo "$tarball: package.json repository.url is '$have', expected '$want' (npm rejects provenance otherwise, E422). Fix Cargo.toml's repository." >&2
            exit 1
        fi
    fi
done

for tarball in "${tarballs[@]}"; do
    name=$(tarball_field "$tarball" name)
    version=$(tarball_field "$tarball" version)
    tag=$(dist_tag "$name" "$version")

    # Re-runnable: a package an earlier attempt already published is skipped
    # (npm rejects a second publish of a version). A staged, unapproved
    # version is invisible here: approve or reject it before re-running.
    published=$(npm_view "$name@$version" version) || exit 1
    if [[ -n $published ]]; then
        echo "$name@$version is already on the registry -- skipping."
        continue
    fi

    echo "Publishing $name@$version under dist-tag '$tag'."
    if [[ -n ${NPM_TOKEN:-} ]]; then
        npmrc=$(mktemp)
        chmod 600 "$npmrc"
        printf '//registry.npmjs.org/:_authToken=%s\n' "$NPM_TOKEN" >"$npmrc"
        args=(--userconfig "$npmrc" --access public --tag "$tag")
        if [[ -n ${ACTIONS_ID_TOKEN_REQUEST_URL:-} ]]; then
            args+=(--provenance)
        fi
        # --userconfig replaces the user npmrc, so the token reaches npm
        # through a file, never the command line or the environment.
        npm stage publish "$tarball" "${args[@]}" || { rm -f "$npmrc"; exit 1; }
        rm -f "$npmrc"
        echo "Staged, NOT yet public: approve $name@$version with 2FA (npm stage list && npm stage approve <stage-id>, or on npmjs.com)."
    else
        npm publish "$tarball" --provenance --access public --tag "$tag"
    fi
done
