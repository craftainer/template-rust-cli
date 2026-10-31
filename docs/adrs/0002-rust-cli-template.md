# 0002. Provide a Rust CLI template that ships binaries from a second workflow

## Status

Accepted

## Context

The first command-line program planned for the craftainer-labs organization
is a Rust CLI that users install in several ways: a downloaded binary, `pip`
or `uvx`, `npm`, Homebrew, a container image and a pre-commit hook. Programs
that follow it need the same quality bar as the library crates and the same
release path. `template-rust-crate` provides the quality gates and a release
that publishes a source crate to crates.io (ADR 0001), but nothing that builds
an executable for macOS or Windows, or packages one for PyPI or npm.

Three constraints shape where that goes:

- `template-base`'s `release.yml` is base-owned (a sync overwrites it) and
  `template-rust-crate`'s `Makefile`, `release.yml` and `scripts/release-*`
  are replace tier. The release builds on one runner per architecture from
  `release-arches`, in jobs that hold no `id-token`, and has no hook for
  runners of other operating systems or for attestations.
- PyPI trusted publishing does not accept a reusable workflow as the
  publisher: the publisher is the top-level workflow file. npm's trusted
  publishers likewise name a workflow file.
- A release created with `GITHUB_TOKEN` starts no other workflow, and
  `workflow_run` is a zizmor `dangerous-triggers` finding that this family
  does not suppress.

The cargo-dist project ("dist") builds archives and checksums reliably, but
its generated CI would replace the release contract, and its npm installer
downloads the binary when the package is installed, which breaks offline
installs.

## Decision

We will create `craftainer/template-rust-cli`, an instance of
`template-rust-crate` that is itself a template, and create every Rust CLI
from it. It leaves the parent's `Makefile`, `release.yml` and
`scripts/release-*` byte-identical, so the contract release stays the
crates.io release of the source crate, and adds, all in its own files:

- a sample program (a library with the logic, a thin `main.rs`, an integration
  test that spawns the binary) that passes every inherited gate;
- `release-binaries.yml`, a `workflow_dispatch`-only workflow with a `tag`
  input, run by the release lander right after `Release`. It guards on the
  default-branch ref, the tag's shape, a published release and the tag being
  in the default branch's history, runs one at a time per tag, and is
  idempotent. It is the caller that holds `uv publish`, `npm publish`, the
  Homebrew commit and the image push, behind the `release` environment and
  the switches `CLI_PYPI_PUBLISH`, `CLI_NPM_PUBLISH`, `HOMEBREW_TAP` and
  `CLI_IMAGE_PUBLISH`, so the trusted publishers can name it;
- `build-binaries.yml`, a reusable workflow it calls, which builds each of six
  targets natively (static `x86_64`/`aarch64-unknown-linux-musl`, both macOS
  targets on `macos-15`, `x86_64`/`aarch64-pc-windows-msvc` on `windows-2025`
  and `windows-11-arm`) and attests archives, wheels, the sdist and the npm
  tarballs with `actions/attest`. Building in a reusable workflow, apart from
  the publishing caller, gives SLSA Build L3 provenance without changing the
  contract. `id-token: write` and `attestations: write` are set in caller and
  callee, because a called workflow cannot hold what its caller withheld;
- dist 0.33.1 as the build, archive and checksum engine only
  (`dist-workspace.toml` with `installers = []` and no `ci`), installed from a
  release asset whose SHA-256 is pinned in `scripts/binaries/release_binaries.py`;
- maturin (`bindings = "bin"`) wheels, one per target, the Linux ones tagged
  for both glibc and musl hosts, plus an sdist, published with `uv publish`
  after `astral-sh/attest-action` generates PEP 740 attestations;
- six `<scope>/<binary>-<os>-<cpu>` npm packages plus a root package with
  exact-pinned `optionalDependencies` and a launcher script, published
  platform packages first, with the family's staged first publish;
- a generated Homebrew formula and a job that commits it to a tap, skipped
  unless `HOMEBREW_TAP` is set; a `scratch` container image of the static
  binary; and a `.pre-commit-hooks.yaml` with `language: rust`;
- tests for the scripts (`scripts/test_release_binaries.py`, run by `prek` in
  CI) and a sync manifest that classifies every new file: the workflows,
  `dist-workspace.toml` and `scripts/binaries/` are `replace`; the CLI's
  identity files (`pyproject.toml`, `packaging.toml`,
  `.pre-commit-hooks.yaml`) are `ignore`.

## Consequences

- A new CLI starts with the full quality bar, a crates.io release and the
  whole binary distribution, and receives fixes through three sync hops
  (`template-base`, `template-rust-crate`, this template). A CLI renames about
  eight places by hand (listed in the README) and nothing in the workflows.
- Releasing is two steps, and forgetting the second is silent: the `Release`
  run is green without binaries. The dispatch is part of the release
  procedure. If the contract later gains an extension point for it, the
  workflow moves into `release.yml` and a manifest tombstone removes the file.
- Because the contract release does not wait for the binaries, a GitHub
  release can exist without them; re-dispatching the workflow with the same tag
  completes it.
- The tools are pinned by hash in a script rather than by a Renovate-managed
  version, so bumping dist or maturin is a deliberate edit of both.
- Linux binaries are static musl: no glibc dependency, one npm package and
  one wheel per CPU for every distribution, but a dependency that needs a C
  library, or the musl allocator's speed, is an instance concern (the sample
  has no dependencies).
- The npm and PyPI publishers name `release-binaries.yml`; renaming it breaks
  both until the publishers are updated.
- The Windows arm64 and Intel macOS builds, and every registry, are proven
  only by the first real dispatch.
