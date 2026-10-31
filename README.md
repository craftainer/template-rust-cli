# template-rust-cli

> [!NOTE]
> This repository is vibe coded with [Claude](https://claude.com/product/claude-code).

A template for a tested, linted and publishable Rust command-line program:
a Cargo workspace with one example program (a library plus a thin binary),
`rustfmt`, `clippy -D warnings`, a 100% line-coverage gate
(`cargo-llvm-cov`), `cargo-deny`, `cargo-semver-checks`, and a release that
publishes the crate to crates.io through trusted publishing and then ships the
binary: native archives for Linux (static), macOS and Windows on x64 and
arm64, wheels on PyPI, npm packages, a Homebrew formula, a container image and
a pre-commit hook, all with build provenance attestations. It is itself an
instance of
[`template-rust-crate`](https://github.com/craftainer/template-rust-crate) (so
it gets the quality gates and the crates.io release from there, and through
it the devcontainer, CI, release workflow and template sync from
[`template-base`](https://github.com/craftainer/template-base)) and is itself a
template: a CLI created from it receives fixes through template sync. See
[`docs/TEMPLATE.md`](docs/TEMPLATE.md) for the conventions shared by every
instance ("Rust CLI template notes" for what this template adds), and
[`docs/adrs/0002-rust-cli-template.md`](docs/adrs/0002-rust-cli-template.md)
for why it is built this way.

## Creating a CLI from this template

1. On GitHub, use "Use this template" on this repository (or
   `gh repo create <org>/<name> --template craftainer/template-rust-cli`).
2. Rename the program. Decide the names first: the repository, the crate and
   binary (usually the same), the PyPI distribution, the npm root package and
   the npm scope for the six platform packages. These places move together:
   - `Cargo.toml`: `[package] name` (`template-rust-cli`), the description,
     `[workspace.package] repository` (it must be the
     `https://github.com/<owner>/<repo>` URL of the repository that
     releases: npm provenance and the Homebrew URLs derive from it), and
     `keywords` and `categories` for crates.io. If the binary should be
     named differently from the crate, add a `[[bin]]` table with its `name`;
   - `src/lib.rs`: the `template_rust_cli::` path in the doctest and the
     program name in `USAGE` and in the `--version` output;
   - `src/main.rs`: the `template_rust_cli::run` path;
   - `tests/cli.rs`: the `CARGO_BIN_EXE_template-rust-cli` variable (it
     carries the binary's name) and every `template_rust_cli::` path;
   - `pyproject.toml`: `[project] name`, `description`, the URL;
   - `packaging.toml`: `[npm] package` (the root npm package, which the CLI's
     users install) and `scope` (the organisation of the platform packages,
     for example `@craftainer-labs`; they become
     `<scope>/<binary>-<os>-<cpu>`);
   - `.pre-commit-hooks.yaml`: the hook `id`, `name`, `description`, `entry`
     and `args`;
   - this file's title and description.

   Then run `cargo update --workspace` to refresh `Cargo.lock` for the new
   name. Nothing else names the CLI: the workflows, `dist-workspace.toml`
   and `scripts/binaries/` read the name from `Cargo.toml` and
   `packaging.toml`, and are template-owned (the container image's
   `/<binary>` included).
3. Point template sync at this template. `.github/template-sync-state.json`
   here records the `template-rust-crate` tag this template itself last synced
   from; the copy in a new CLI must instead name this repository and the tag
   it was created at (the latest release of `craftainer/template-rust-cli`):

   ```json
   {"template_repo": "craftainer/template-rust-cli", "synced_tag": "vX.Y.Z"}
   ```

   Add a `TEMPLATE_SYNC_TOKEN` secret too (see `.github/workflows/README.md`,
   "Template sync"). Files the CLI owns beyond the manifest's `ignore`
   tier go in `.github/template-sync-manifest.local.yml` under `ignore:`.
4. Open the repository in the devcontainer, then run
   `prek run --all-files --hook-stage manual`; everything should pass before
   you write any code.
5. Keep `LICENSE` (GPL-3.0) and `deny.toml`'s license allow-list, or change
   both together (and `license` in `Cargo.toml` and `pyproject.toml`).

## Publishing to crates.io

`make publish-packages` is a no-op (with a `::warning::` in the log) until you opt in, per repository, by setting the
repository variable `CARGO_PUBLISH` to any non-empty value (Settings,
Secrets and variables, Actions, Variables). The version lives only in the
git tag `release.yml` computes; `Cargo.toml` stays at `0.0.0` and
`make build` stamps `RELEASE_VERSION` into the build without committing it.

crates.io has no "pending publisher": a trusted publisher can only be added
to a crate that already exists. So the first release of each crate uses a
short-lived bootstrap token, and trusted publishing takes over after it:

1. Create a crates.io API token scoped to the new crate name
   (`publish-new` only for the first release) with a short expiry, and store
   it as the repository secret `CARGO_REGISTRY_TOKEN`.
2. Set `CARGO_PUBLISH=1` and run the release workflow. `make publish-packages` sees
   the token and publishes with it.
3. On crates.io, open the crate's Settings, Trusted Publishing, and add a
   GitHub publisher: the owner, the repository, the workflow `release.yml`,
   and no environment.
4. Delete the `CARGO_REGISTRY_TOKEN` secret and revoke the token. From the
   next release, `make publish-packages` finds no token and exchanges the job's
   GitHub OIDC identity for a 30-minute crates.io token, publishes with it
   and revokes it.

## Releasing the binaries

The `Release` workflow (above) does not build the binaries. Right after it
succeeds, dispatch the second workflow with the same tag, and watch it:

```sh
gh workflow run release-binaries.yml -f tag=vX.Y.Z
gh run watch
```

It builds the six targets on native runners, attests everything
([SLSA Build L3](https://slsa.dev/spec/v1.0/levels), verify with
`gh attestation verify <file> --repo <owner>/<repo>`), attaches the archives,
their checksums and the Homebrew formula to the GitHub release, and then
publishes to whichever of these you switched on with a repository variable:

| Variable | Publishes | Needs |
| --- | --- | --- |
| `CLI_PYPI_PUBLISH` = `pypi` or `testpypi` | wheels and sdist, with PEP 740 attestations | a PyPI trusted publisher (a pending one works for the first release) naming workflow `release-binaries.yml` and environment `release` |
| `CLI_NPM_PUBLISH` = any value | the six `<scope>/<binary>-<os>-<cpu>` packages, then the root package | an npm trusted publisher per package naming `release-binaries.yml` and `release`; the first publish of all seven is staged with the stage-only secret `NPM_TOKEN` and approved by hand, as for the family's npm packages |
| `HOMEBREW_TAP` = `<owner>/homebrew-<tap>` | commits `Formula/<binary>.rb` to the tap | the secret `HOMEBREW_TAP_TOKEN` (write access to the tap) |
| `CLI_IMAGE_PUBLISH` = any value | `ghcr.io/<owner>/<repo>:<version>` (and `latest` for a stable release), multi-arch | nothing beyond `GITHUB_TOKEN` |

Create the GitHub environment `release` first (the PyPI, npm and Homebrew
jobs run in it, so you can add required reviewers there). The workflow only
runs from the default branch, for a published release whose tag is on it, and
can be re-run for the same tag. Details, and why it is a manual second step,
are in `docs/TEMPLATE.md`, "Rust CLI template notes".

Using the CLI as a hook in other repositories (`.pre-commit-hooks.yaml`):

```yaml
repos:
  - repo: https://github.com/<owner>/<repo>
    rev: vX.Y.Z
    hooks:
      - id: <hook id>
```

## License

Licensed under the [GNU General Public License v3.0](LICENSE).
