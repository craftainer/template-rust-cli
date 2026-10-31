#!/usr/bin/env python3
"""Packaging helpers for the Rust CLI's binary release.

One implementation for .github/workflows/build-binaries.yml and
release-binaries.yml, so the target list, the archive layout and the package
names can't drift between the jobs that use them. Standard library only; run
with `python3 -I scripts/binaries/release_binaries.py <command> ...`.

Commands:
  install-tool {dist,maturin} DEST   download the pinned release of the tool,
                                     verify its SHA-256, put the executable in
                                     DEST and print DEST
  smoke ARCHIVE VERSION              unpack a dist archive and check that the
                                     binary reports VERSION from --version
  npm --version V --archives DIR --out DIR
                                     write one npm package directory per
                                     target (and the root package) under OUT;
                                     print the directories, platform packages
                                     first and the root package last
  brew --version V --archives DIR --out FILE
                                     write the Homebrew formula
  stage-image --archives DIR --out DIR
                                     lay out the Linux binaries and a Dockerfile
                                     rendered from scripts/binaries/Dockerfile
                                     for the binary's name as the Docker build
                                     context (build it with its own Dockerfile)

DIR for --archives holds dist's `<binary>-<target>.(tar.gz|zip)` archives and
their `.sha256` files. Project facts come from Cargo.toml and packaging.toml
at the repository root (override with ROOT).
"""

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ROOT") or HERE.parent.parent)

# Pinned tool releases, one SHA-256 per host asset (the digest GitHub computed
# at upload, from the release API). Bumping a tool means changing the version
# here and the hashes with it, and dist-workspace.toml's `cargo-dist-version`
# or pyproject.toml's `requires` to match. Windows arm64 runs dist's x86_64
# build (Windows 11 on Arm emulates x64).
TOOLS = {
    "dist": {
        "version": "0.33.1",
        "url": "https://github.com/axodotdev/cargo-dist/releases/download/v{version}/{asset}",
        "binary": "dist",
        "assets": {
            ("Linux", "x86_64"): ("cargo-dist-x86_64-unknown-linux-gnu.tar.xz", "da68e159d5fed1ee61b00284da444e719927dbd0b7d0e2c34e57a04414453cf9"),
            ("Linux", "arm64"): ("cargo-dist-aarch64-unknown-linux-gnu.tar.xz", "e883f4c6c17f2458d6ced950d978c04b45423ed3a5fdbebcb8f8de568389e1c1"),
            ("Darwin", "x86_64"): ("cargo-dist-x86_64-apple-darwin.tar.xz", "e9dc303035fbb2f44295948e0a076f1d1d218a0328e9b84e16955863bba92bf4"),
            ("Darwin", "arm64"): ("cargo-dist-aarch64-apple-darwin.tar.xz", "b8bf041a38e9e54ecf41eee2574ca6644043796620bb0ee8340e9c0acbaaae8f"),
            ("Windows", "x86_64"): ("cargo-dist-x86_64-pc-windows-msvc.zip", "79dfcce84e6a8e69210f11fc23d445c3aac7af6a29e0e981867f053c0a869316"),
            ("Windows", "arm64"): ("cargo-dist-x86_64-pc-windows-msvc.zip", "79dfcce84e6a8e69210f11fc23d445c3aac7af6a29e0e981867f053c0a869316"),
        },
    },
    "maturin": {
        "version": "1.15.0",
        "url": "https://github.com/PyO3/maturin/releases/download/v{version}/{asset}",
        "binary": "maturin",
        "assets": {
            ("Linux", "x86_64"): ("maturin-x86_64-unknown-linux-musl.tar.gz", "5a7d5226e83598b0c85881a0a03be16393ee2c0924b6f9d752951795825ac806"),
            ("Linux", "arm64"): ("maturin-aarch64-unknown-linux-musl.tar.gz", "cd287a2c9dc69ab559670d245bfc9d3f5b0c9f184aa98ad0d8f826abc90c78dd"),
            ("Darwin", "x86_64"): ("maturin-x86_64-apple-darwin.tar.gz", "d72809b92806a53bdc22074aeedc83af66f8bdb391e2c60bc72547c40ec10629"),
            ("Darwin", "arm64"): ("maturin-aarch64-apple-darwin.tar.gz", "d7a9ce82389e75aeaef8fdda43d35dbc956fac6a4b143a4c7b447b6ffadc7eaf"),
            ("Windows", "x86_64"): ("maturin-x86_64-pc-windows-msvc.zip", "c645dc55f848205210552e77f744685c90c35ae81fd03e47a9887ebdc8ae8299"),
            ("Windows", "arm64"): ("maturin-aarch64-pc-windows-msvc.zip", "bbb9361d2101039a81423cd3d66d6b3e94365640104bb553ee25591989010953"),
        },
    },
}

MACHINE_ALIASES = {"x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}


class Error(Exception):
    """A message for the user; the command exits 1 with it."""


# --- project facts -----------------------------------------------------------


def load_targets() -> list[dict]:
    with open(HERE / "targets.json", encoding="utf-8") as handle:
        return json.load(handle)["targets"]


def load_project(root: Path = ROOT) -> dict:
    """Name, binary, description, license, repository, keywords and npm names."""
    with open(root / "Cargo.toml", "rb") as handle:
        manifest = tomllib.load(handle)
    package = manifest["package"]
    workspace = manifest.get("workspace", {}).get("package", {})

    def field(key: str, default=None):
        value = package.get(key, default)
        if isinstance(value, dict) and value.get("workspace") is True:
            return workspace.get(key, default)
        return value

    bins = manifest.get("bin") or []
    with open(root / "packaging.toml", "rb") as handle:
        packaging = tomllib.load(handle)
    npm = packaging["npm"]
    scope = npm["scope"]
    if not re.fullmatch(r"@[a-z0-9][a-z0-9._-]*", scope):
        raise Error(f"packaging.toml: npm.scope must look like @org, got {scope!r}")
    return {
        "name": package["name"],
        "binary": bins[0]["name"] if bins else package["name"],
        "description": field("description", ""),
        "license": field("license", ""),
        "repository": field("repository", ""),
        "keywords": field("keywords", []),
        "npm_package": npm["package"],
        "npm_scope": scope,
    }


def github_slug(repository: str) -> str:
    """`owner/repo` from a github.com repository URL."""
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?", repository)
    if not match:
        raise Error(f"Cargo.toml repository must be a https://github.com/<owner>/<repo> URL, got {repository!r}")
    return match.group(1)


def archive_name(binary: str, target: dict) -> str:
    return f"{binary}-{target['target']}.{target['archive']}"


def platform_package(project: dict, target: dict) -> str:
    return f"{project['npm_scope']}/{project['binary']}-{target['npm_os']}-{target['npm_cpu']}"


# --- archives ----------------------------------------------------------------


def extract_binary(archive: Path, name: str) -> bytes:
    """The file called `name` (at any depth) in a dist tar.gz or zip archive."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            hits = [m for m in zf.namelist() if not m.endswith("/") and Path(m).name == name]
            if len(hits) != 1:
                raise Error(f"{archive.name}: expected exactly one {name}, found {hits}")
            return zf.read(hits[0])
    with tarfile.open(archive, "r:*") as tf:
        hits = [m for m in tf.getmembers() if m.isfile() and Path(m.name).name == name]
        if len(hits) != 1:
            raise Error(f"{archive.name}: expected exactly one {name}, found {[m.name for m in hits]}")
        handle = tf.extractfile(hits[0])
        assert handle is not None
        return handle.read()


def write_executable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def read_sha256_file(path: Path, expected_name: str) -> str:
    """The digest in a `sha256sum`-style file, checked to name `expected_name`."""
    parts = path.read_text(encoding="utf-8").split()
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
        raise Error(f"{path.name}: not a sha256sum line")
    if parts[1].lstrip("*") != expected_name:
        raise Error(f"{path.name}: names {parts[1]!r}, expected {expected_name!r}")
    return parts[0]


# --- install-tool --------------------------------------------------------------


def select_asset(tool: str, system: str, machine: str) -> tuple[str, str]:
    entry = TOOLS[tool]
    key = (system, MACHINE_ALIASES.get(machine.lower(), machine.lower()))
    if key not in entry["assets"]:
        raise Error(f"no pinned {tool} {entry['version']} for {system}/{machine}")
    return entry["assets"][key]


def download(url: str, attempts: int = 4) -> bytes:
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - https URL from the table above
                return response.read()
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == attempts:
                raise Error(f"download failed: {url}: {error}") from error
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def unpack_executable(data: bytes, asset: str, binary: str, dest: Path) -> Path:
    """Writes the file `binary` (or `binary.exe`) from the archive bytes into dest."""
    names = {binary, binary + ".exe"}
    found: dict[str, bytes] = {}
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if not member.endswith("/") and Path(member).name in names:
                    found[member] = zf.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for member in tf.getmembers():
                handle = tf.extractfile(member) if member.isfile() else None
                if handle is not None and Path(member.name).name in names:
                    found[member.name] = handle.read()
    if len(found) != 1:
        raise Error(f"{asset}: expected exactly one {binary} executable, found {sorted(found)}")
    ((member, content),) = found.items()
    out = dest / Path(member).name
    write_executable(out, content)
    return out


def install_tool(tool: str, dest: Path, system: str | None = None, machine: str | None = None) -> Path:
    asset, expected = select_asset(tool, system or platform.system(), machine or platform.machine())
    entry = TOOLS[tool]
    url = entry["url"].format(version=entry["version"], asset=asset)
    data = download(url)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise Error(f"{asset}: SHA-256 is {actual}, expected {expected}")
    dest.mkdir(parents=True, exist_ok=True)
    unpack_executable(data, asset, entry["binary"], dest)
    return dest


# --- smoke ---------------------------------------------------------------------


def smoke(archive: Path, version: str, binary: str) -> str:
    exe = ".exe" if archive.suffix == ".zip" else ""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / (binary + exe)
        write_executable(path, extract_binary(archive, binary + exe))
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, check=False, timeout=60)
    if result.returncode != 0:
        raise Error(f"{archive.name}: --version exited {result.returncode}: {result.stderr.strip()}")
    if version not in result.stdout:
        raise Error(f"{archive.name}: --version printed {result.stdout.strip()!r}, expected it to contain {version}")
    return result.stdout.strip()


# --- npm -----------------------------------------------------------------------


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def npm_repository(project: dict) -> dict:
    return {"type": "git", "url": f"git+https://github.com/{github_slug(project['repository'])}.git"}


def build_npm(version: str, archives: Path, out: Path, project: dict, root: Path = ROOT) -> list[Path]:
    binary = project["binary"]
    slug = github_slug(project["repository"])
    common = {
        "version": version,
        "license": project["license"],
        "repository": npm_repository(project),
        "homepage": f"https://github.com/{slug}#readme",
        "bugs": {"url": f"https://github.com/{slug}/issues"},
    }
    ordered: list[Path] = []
    optional: dict[str, str] = {}
    for target in load_targets():
        name = platform_package(project, target)
        optional[name] = version
        pkg_dir = out / f"{binary}-{target['npm_os']}-{target['npm_cpu']}"
        exe = binary + target["exe"]
        archive = archives / archive_name(binary, target)
        if not archive.is_file():
            raise Error(f"missing archive: {archive}")
        write_executable(pkg_dir / "bin" / exe, extract_binary(archive, exe))
        shutil.copyfile(root / "LICENSE", pkg_dir / "LICENSE")
        # No `libc` field on Linux: the binaries are static musl, so one
        # package serves glibc and musl hosts (npm installs a package with a
        # `libc` field only where the host's libc matches).
        write_json(
            pkg_dir / "package.json",
            {
                "name": name,
                "description": f"The {target['target']} binary of {binary}. Install {project['npm_package']} instead.",
                **common,
                "os": [target["npm_os"]],
                "cpu": [target["npm_cpu"]],
                "files": ["bin/", "LICENSE"],
            },
        )
        ordered.append(pkg_dir)
    root_dir = out / "root"
    launcher = root_dir / "bin" / f"{binary}.js"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "launcher.js", launcher)
    launcher.chmod(0o755)
    shutil.copyfile(root / "LICENSE", root_dir / "LICENSE")
    shutil.copyfile(root / "README.md", root_dir / "README.md")
    manifest = {
        "name": project["npm_package"],
        "description": project["description"],
        **common,
        "keywords": project["keywords"],
        "bin": {binary: f"bin/{binary}.js"},
        "files": ["bin/", "LICENSE", "README.md"],
        "engines": {"node": ">=18"},
        "optionalDependencies": optional,
    }
    write_json(root_dir / "package.json", manifest)
    ordered.append(root_dir)
    return ordered


# --- homebrew ------------------------------------------------------------------


def formula_class(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[-_.]", name) if part)


def ruby_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False).replace("#{", "\\#{")


def build_formula(version: str, archives: Path, project: dict) -> str:
    binary = project["binary"]
    slug = github_slug(project["repository"])
    by_target = {t["target"]: t for t in load_targets()}

    def stanza(target: str, indent: str) -> str:
        entry = by_target[target]
        archive = archive_name(binary, entry)
        digest = read_sha256_file(archives / f"{archive}.sha256", archive)
        url = f"https://github.com/{slug}/releases/download/v{version}/{archive}"
        return f'{indent}url "{url}"\n{indent}sha256 "{digest}"\n'

    parts = [
        f"class {formula_class(binary)} < Formula\n",
        f"  desc {ruby_string(project['description'])}\n",
        f'  homepage "https://github.com/{slug}"\n',
        f'  version "{version}"\n',
        f'  license "{project["license"]}"\n\n',
    ]
    for block, os_name in (("on_macos", "apple-darwin"), ("on_linux", "unknown-linux-musl")):
        parts.append(f"  {block} do\n")
        for hook, cpu in (("on_arm", "aarch64"), ("on_intel", "x86_64")):
            parts.append(f"    {hook} do\n")
            parts.append(stanza(f"{cpu}-{os_name}", "      "))
            parts.append("    end\n")
        parts.append("  end\n\n")
    parts.append(
        f"  def install\n    bin.install \"{binary}\"\n  end\n\n"
        f"  test do\n    assert_match version.to_s, shell_output(\"#{{bin}}/{binary} --version\")\n  end\nend\n"
    )
    return "".join(parts)


# --- container image -----------------------------------------------------------


def stage_image(archives: Path, out: Path, project: dict) -> list[Path]:
    binary = project["binary"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", binary):
        raise Error(f"the binary name {binary!r} cannot go into a Dockerfile; use letters, digits, '.', '_' and '-'")
    out.mkdir(parents=True, exist_ok=True)
    staged = []
    for target in load_targets():
        if not target["target"].endswith("unknown-linux-musl"):
            continue
        arch = {"x64": "amd64", "arm64": "arm64"}[target["npm_cpu"]]
        dest = out / f"linux-{arch}" / binary
        write_executable(dest, extract_binary(archives / archive_name(binary, target), binary))
        staged.append(dest)
    dockerfile = (Path(__file__).resolve().parent / "Dockerfile").read_text(encoding="utf-8")
    (out / "Dockerfile").write_text(dockerfile.replace("@BINARY@", binary), encoding="utf-8")
    return staged


# --- command line ----------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("install-tool")
    p.add_argument("tool", choices=sorted(TOOLS))
    p.add_argument("dest", type=Path)
    p = sub.add_parser("smoke")
    p.add_argument("archive", type=Path)
    p.add_argument("version")
    p = sub.add_parser("npm")
    p.add_argument("--version", required=True)
    p.add_argument("--archives", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("brew")
    p.add_argument("--version", required=True)
    p.add_argument("--archives", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("stage-image")
    p.add_argument("--archives", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "install-tool":
            print(install_tool(args.tool, args.dest))
        elif args.command == "smoke":
            print(smoke(args.archive, args.version, load_project()["binary"]))
        elif args.command == "npm":
            for directory in build_npm(args.version, args.archives, args.out, load_project()):
                print(directory)
        elif args.command == "brew":
            project = load_project()
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(build_formula(args.version, args.archives, project), encoding="utf-8")
        else:
            for path in stage_image(args.archives, args.out, load_project()):
                print(path)
    except Error as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
