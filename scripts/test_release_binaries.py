#!/usr/bin/env python3
"""Tests for the binary-release tooling: scripts/binaries/ and its workflows.

Run with `python3 -I scripts/test_release_binaries.py`. None of it needs Rust,
npm, a network or a container: archives are built in a temporary directory, a
fake `npm` stands in for the registry, and the workflows are checked as text.
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "scripts" / "binaries"
WORKFLOWS = ROOT / ".github" / "workflows"

spec = importlib.util.spec_from_file_location("release_binaries", BIN / "release_binaries.py")
rb = importlib.util.module_from_spec(spec)
sys.modules["release_binaries"] = rb
spec.loader.exec_module(rb)

FAKE_BINARY = b"#!/bin/sh\necho 'template-rust-cli 1.2.3'\n"


def run(cmd, env=None, **kw):
    return subprocess.run(
        cmd, cwd=ROOT, env={**os.environ, **(env or {})}, capture_output=True, text=True, check=False, **kw
    )


def make_archive(directory: Path, target: dict, binary: str = "template-rust-cli", payload: bytes = FAKE_BINARY):
    """A dist-shaped archive (one top-level directory) plus its .sha256 file."""
    name = rb.archive_name(binary, target)
    path = directory / name
    inner = f"{binary}-{target['target']}"
    exe = binary + target["exe"]
    if target["archive"] == "zip":
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(f"{inner}/{exe}", payload)
            zf.writestr(f"{inner}/README.md", "readme")
    else:
        with tarfile.open(path, "w:gz") as tf:
            info = tarfile.TarInfo(f"{inner}/{exe}")
            info.size = len(payload)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(payload))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (directory / f"{name}.sha256").write_text(f"{digest} *{name}\n", encoding="utf-8")
    return path, digest


def make_all_archives(directory: Path, binary: str = "template-rust-cli"):
    return {t["target"]: make_archive(directory, t, binary) for t in rb.load_targets()}


class Targets(unittest.TestCase):
    """The target list is stated in four places; they must agree."""

    def setUp(self):
        self.targets = rb.load_targets()
        self.triples = [t["target"] for t in self.targets]

    def test_dist_workspace_targets_match(self):
        config = tomllib.loads((ROOT / "dist-workspace.toml").read_text(encoding="utf-8"))
        self.assertEqual(sorted(config["dist"]["targets"]), sorted(self.triples))

    def test_dist_runs_as_a_build_engine_only(self):
        config = tomllib.loads((ROOT / "dist-workspace.toml").read_text(encoding="utf-8"))["dist"]
        self.assertEqual(config["installers"], [])
        self.assertNotIn("ci", config)
        self.assertEqual(config["cargo-dist-version"], rb.TOOLS["dist"]["version"])

    def test_build_matrix_matches(self):
        text = (WORKFLOWS / "build-binaries.yml").read_text(encoding="utf-8")
        rows = re.findall(
            r"- target: (\S+)\n\s+runner: (\S+)\n\s+archive: (\S+)\n", text
        )
        self.assertEqual(
            sorted(rows),
            sorted((t["target"], t["runner"], t["archive"]) for t in self.targets),
        )

    def test_every_target_has_a_consistent_row(self):
        for t in self.targets:
            with self.subTest(target=t["target"]):
                windows = "windows" in t["target"]
                self.assertEqual(t["archive"], "zip" if windows else "tar.gz")
                self.assertEqual(t["exe"], ".exe" if windows else "")
                want_os = "win32" if windows else "darwin" if "apple" in t["target"] else "linux"
                self.assertEqual(t["npm_os"], want_os)
                self.assertIn(t["npm_cpu"], {"x64", "arm64"})
                self.assertEqual(t["npm_cpu"] == "arm64", t["target"].startswith("aarch64"))
                if "linux" in t["target"]:
                    self.assertTrue(t["target"].endswith("-musl"), "Linux binaries are static musl")

    def test_pyproject_requires_the_pinned_maturin(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["tool"]["maturin"]["bindings"], "bin")
        self.assertEqual(pyproject["build-system"]["requires"], ["maturin>=1.15,<2"])
        major, minor, *_ = (int(p) for p in rb.TOOLS["maturin"]["version"].split("."))
        self.assertEqual((major, minor >= 15), (1, True))


class Workflows(unittest.TestCase):
    def text(self, name):
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def test_release_binaries_is_dispatch_only(self):
        text = self.text("release-binaries.yml")
        on = re.search(r"^on:\n((?:  .*\n|\n)+)", text, re.MULTILINE).group(1)
        self.assertEqual(re.findall(r"^  (\w+):", on, re.MULTILINE), ["workflow_dispatch"])
        self.assertIn("tag:", on)
        self.assertNotIn("workflow_run", re.sub(r"#.*", "", text))

    def test_build_binaries_is_reusable_only(self):
        text = self.text("build-binaries.yml")
        on = re.search(r"^on:\n((?:  .*\n|\n)+)", text, re.MULTILINE).group(1)
        self.assertEqual(re.findall(r"^  (\w+):", on, re.MULTILINE), ["workflow_call"])

    def test_every_action_is_pinned_by_sha(self):
        for name in ("release-binaries.yml", "build-binaries.yml"):
            for use in re.findall(r"uses: (\S+)", self.text(name)):
                with self.subTest(workflow=name, uses=use):
                    if use.startswith(("./", "$/")):  # this repository, at the running commit
                        continue
                    self.assertRegex(use, r"@[0-9a-f]{40}$")

    def test_attestation_permissions_are_set_in_caller_and_callee(self):
        caller, callee = self.text("release-binaries.yml"), self.text("build-binaries.yml")
        for text in (caller, callee):
            self.assertIn("id-token: write", text)
            self.assertIn("attestations: write", text)
        build_job = re.search(r"\n  build:\n(.*?)\n  \w", caller, re.DOTALL).group(1)
        self.assertIn("id-token: write", build_job)
        self.assertIn("attestations: write", build_job)
        self.assertIn("uses: $/.github/workflows/build-binaries.yml", build_job)

    def test_publishing_stays_in_the_caller_behind_the_release_environment(self):
        caller, callee = self.text("release-binaries.yml"), self.text("build-binaries.yml")
        self.assertNotIn("uv publish", callee)
        self.assertNotIn("npm publish", callee)
        self.assertIn("uv publish", caller)
        self.assertIn("publish-npm.sh", caller)
        for job in ("pypi", "npm", "homebrew"):
            body = re.search(rf"\n  {job}:\n(.*?)(?=\n  \w[\w-]*:\n|\Z)", caller, re.DOTALL).group(1)
            with self.subTest(job=job):
                self.assertIn("environment: release", body)
        self.assertIn("vars.CLI_PYPI_PUBLISH != ''", caller)
        self.assertIn("vars.CLI_NPM_PUBLISH != ''", caller)
        self.assertIn("vars.HOMEBREW_TAP != ''", caller)
        self.assertIn("vars.CLI_IMAGE_PUBLISH != ''", caller)

    def test_one_run_per_tag_and_no_secrets_inherited(self):
        caller = self.text("release-binaries.yml")
        self.assertIn("group: release-binaries-${{ inputs.tag }}", caller)
        self.assertNotIn("secrets: inherit", caller)

    def test_contract_files_of_the_parent_are_untouched(self):
        # The parent's release.yml must stay free of this template's extras.
        self.assertNotIn("binaries", self.text("release.yml"))
        self.assertNotIn("binaries", (ROOT / "Makefile").read_text(encoding="utf-8"))

    def test_hook_manifest_and_hook_id(self):
        text = (ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^- id: \S+\n")
        self.assertRegex(text, r"(?m)^  language: rust$")


class InstallTool(unittest.TestCase):
    def test_tables_are_well_formed(self):
        for tool, entry in rb.TOOLS.items():
            for (system, machine), (asset, digest) in entry["assets"].items():
                with self.subTest(tool=tool, host=(system, machine)):
                    self.assertRegex(digest, r"^[0-9a-f]{64}$")
                    self.assertTrue(asset.endswith(".zip" if system == "Windows" else (".tar.xz", ".tar.gz")[tool == "maturin"]))

    def test_every_runner_has_an_asset(self):
        hosts = [("Linux", "x86_64"), ("Linux", "aarch64"), ("Darwin", "arm64"), ("Windows", "AMD64"), ("Windows", "ARM64")]
        for tool in rb.TOOLS:
            for system, machine in hosts:
                with self.subTest(tool=tool, host=(system, machine)):
                    asset, _ = rb.select_asset(tool, system, machine)
                    self.assertIn(tool if tool == "maturin" else "dist", asset)

    def test_unknown_host_is_refused(self):
        with self.assertRaises(rb.Error):
            rb.select_asset("dist", "FreeBSD", "amd64")

    def archive_for(self, tool, system, machine, payload=b"exe"):
        asset, _ = rb.select_asset(tool, system, machine)
        binary = rb.TOOLS[tool]["binary"]
        buf = io.BytesIO()
        if asset.endswith(".zip"):
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr(f"x/{binary}.exe", payload)
        else:
            mode = "w:xz" if asset.endswith(".xz") else "w:gz"
            with tarfile.open(fileobj=buf, mode=mode) as tf:
                info = tarfile.TarInfo(f"x/{binary}")
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
        return buf.getvalue()

    def install(self, tool, system, machine, data, expected=None):
        asset, digest = rb.select_asset(tool, system, machine)
        entry = {**rb.TOOLS[tool], "assets": {(system, rb.MACHINE_ALIASES[machine.lower()]): (asset, expected or hashlib.sha256(data).hexdigest())}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(rb.TOOLS, {tool: entry}), mock.patch.object(
            rb, "download", return_value=data
        ):
            dest = rb.install_tool(tool, Path(tmp) / "bin", system, machine)
            return sorted(p.name for p in dest.iterdir()), os.access(dest / sorted(os.listdir(dest))[0], os.X_OK)

    def test_installs_a_verified_tool_from_each_archive_kind(self):
        for tool, system, machine, name in [
            ("dist", "Linux", "x86_64", "dist"),
            ("maturin", "Linux", "aarch64", "maturin"),
            ("dist", "Windows", "ARM64", "dist.exe"),
        ]:
            with self.subTest(tool=tool, host=(system, machine)):
                names, executable = self.install(tool, system, machine, self.archive_for(tool, system, machine))
                self.assertEqual(names, [name])
                self.assertTrue(executable)

    def test_a_wrong_checksum_is_refused(self):
        with self.assertRaisesRegex(rb.Error, "SHA-256"):
            self.install("dist", "Linux", "x86_64", self.archive_for("dist", "Linux", "x86_64"), expected="0" * 64)


class CheckDispatch(unittest.TestCase):
    def check(self, tag, ref="refs/heads/main", default="main"):
        return run(
            ["scripts/binaries/check-dispatch.sh"],
            env={"RELEASE_TAG": tag, "GITHUB_REF": ref, "DEFAULT_BRANCH": default},
        )

    def test_stable_tag(self):
        r = self.check("v1.2.3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["version=1.2.3", "prerelease=false"])

    def test_prerelease_tag(self):
        r = self.check("v1.2.3-rc.1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["version=1.2.3-rc.1", "prerelease=true"])

    def test_other_branch_is_refused(self):
        r = self.check("v1.2.3", ref="refs/heads/feature")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("default branch", r.stderr)

    def test_a_tag_ref_is_refused(self):
        self.assertNotEqual(self.check("v1.2.3", ref="refs/tags/v1.2.3").returncode, 0)

    def test_bad_tags_are_refused(self):
        for tag in ["1.2.3", "v1.2", "v1.2.3.4", "v01.2.3", "v1.2.3-", "main", "v1.2.3 ", "v1.2.3;id", "$(id)", ""]:
            with self.subTest(tag=tag):
                self.assertNotEqual(self.check(tag).returncode, 0)


class Project(unittest.TestCase):
    def test_repository_facts(self):
        project = rb.load_project()
        self.assertEqual(project["binary"], project["name"])
        self.assertTrue(project["npm_scope"].startswith("@"))
        self.assertEqual(rb.github_slug(project["repository"]).count("/"), 1)

    def test_a_bin_table_names_the_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Cargo.toml").write_text(
                '[package]\nname = "a"\nversion = "1.0.0"\nlicense = "MIT"\nrepository = "https://github.com/o/r"\n'
                '[[bin]]\nname = "b"\npath = "src/main.rs"\n'
            )
            (Path(tmp) / "packaging.toml").write_text('[npm]\npackage = "a"\nscope = "@o"\n')
            self.assertEqual(rb.load_project(Path(tmp))["binary"], "b")

    def test_bad_repository_and_scope_are_refused(self):
        with self.assertRaises(rb.Error):
            rb.github_slug("https://gitlab.com/o/r")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Cargo.toml").write_text('[package]\nname = "a"\nversion = "1.0.0"\n')
            (Path(tmp) / "packaging.toml").write_text('[npm]\npackage = "a"\nscope = "o"\n')
            with self.assertRaises(rb.Error):
                rb.load_project(Path(tmp))


class Archives(unittest.TestCase):
    def test_extract_binary_from_tar_and_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            for target in rb.load_targets():
                path, _ = make_archive(Path(tmp), target)
                self.assertEqual(rb.extract_binary(path, "template-rust-cli" + target["exe"]), FAKE_BINARY)

    def test_missing_binary_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = make_archive(Path(tmp), rb.load_targets()[0])
            with self.assertRaises(rb.Error):
                rb.extract_binary(path, "nope")

    def test_smoke_runs_the_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = make_archive(Path(tmp), rb.load_targets()[0])
            self.assertEqual(rb.smoke(path, "1.2.3", "template-rust-cli"), "template-rust-cli 1.2.3")
            with self.assertRaisesRegex(rb.Error, "expected it to contain 9.9.9"):
                rb.smoke(path, "9.9.9", "template-rust-cli")

    def test_smoke_reports_a_failing_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = make_archive(Path(tmp), rb.load_targets()[0], payload=b"#!/bin/sh\nexit 3\n")
            with self.assertRaisesRegex(rb.Error, "exited 3"):
                rb.smoke(path, "1.2.3", "template-rust-cli")

    def test_sha256_file_must_name_its_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.sha256"
            path.write_text("a" * 64 + " *other.tar.gz\n")
            with self.assertRaisesRegex(rb.Error, "names"):
                rb.read_sha256_file(path, "x.tar.gz")
            path.write_text("nothex *x.tar.gz\n")
            with self.assertRaisesRegex(rb.Error, "not a sha256sum"):
                rb.read_sha256_file(path, "x.tar.gz")


class NpmPackages(unittest.TestCase):
    def build(self, version="1.2.3"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        archives, out = Path(tmp.name) / "archives", Path(tmp.name) / "out"
        archives.mkdir()
        make_all_archives(archives)
        dirs = rb.build_npm(version, archives, out, rb.load_project())
        return dirs, out

    def manifest(self, directory):
        return json.loads((directory / "package.json").read_text())

    def test_six_platform_packages_then_the_root(self):
        dirs, out = self.build()
        self.assertEqual(len(dirs), 7)
        self.assertEqual(dirs[-1], out / "root")
        self.assertEqual(
            [self.manifest(d)["name"] for d in dirs[:-1]],
            [
                "@craftainer/template-rust-cli-linux-x64",
                "@craftainer/template-rust-cli-linux-arm64",
                "@craftainer/template-rust-cli-darwin-x64",
                "@craftainer/template-rust-cli-darwin-arm64",
                "@craftainer/template-rust-cli-win32-x64",
                "@craftainer/template-rust-cli-win32-arm64",
            ],
        )

    def test_platform_package_fields(self):
        dirs, _ = self.build()
        for directory in dirs[:-1]:
            manifest = self.manifest(directory)
            with self.subTest(package=manifest["name"]):
                self.assertEqual(manifest["version"], "1.2.3")
                self.assertEqual(len(manifest["os"]), 1)
                self.assertEqual(len(manifest["cpu"]), 1)
                self.assertTrue(manifest["name"].endswith(f"-{manifest['os'][0]}-{manifest['cpu'][0]}"))
                # Static musl: no libc field, so glibc and musl hosts both install it.
                self.assertNotIn("libc", manifest)
                self.assertEqual(manifest["license"], "GPL-3.0-only")
                self.assertTrue((directory / "LICENSE").is_file())
                exe = "template-rust-cli" + (".exe" if manifest["os"] == ["win32"] else "")
                binary = directory / "bin" / exe
                self.assertEqual(binary.read_bytes(), FAKE_BINARY)
                self.assertTrue(binary.stat().st_mode & stat.S_IXUSR)

    def test_root_package_pins_every_platform_package_exactly(self):
        dirs, _ = self.build("1.2.3-rc.1")
        root = self.manifest(dirs[-1])
        self.assertEqual(root["name"], "template-rust-cli")
        self.assertEqual(root["bin"], {"template-rust-cli": "bin/template-rust-cli.js"})
        self.assertEqual(
            root["optionalDependencies"], {self.manifest(d)["name"]: "1.2.3-rc.1" for d in dirs[:-1]}
        )
        self.assertEqual(root["files"], ["bin/", "LICENSE", "README.md"])
        self.assertNotIn("postinstall", json.dumps(root))
        self.assertNotIn("scripts", root)
        launcher = dirs[-1] / "bin" / "template-rust-cli.js"
        self.assertEqual(launcher.read_bytes(), (BIN / "launcher.js").read_bytes())
        self.assertTrue(launcher.stat().st_mode & stat.S_IXUSR)

    def test_repository_matches_the_github_repository(self):
        dirs, _ = self.build()
        slug = rb.github_slug(rb.load_project()["repository"])
        for directory in dirs:
            self.assertEqual(self.manifest(directory)["repository"]["url"], f"git+https://github.com/{slug}.git")

    def test_a_missing_archive_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(rb.Error, "missing archive"):
                rb.build_npm("1.0.0", Path(tmp), Path(tmp) / "out", rb.load_project())

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_launcher_runs_the_platform_binary(self):
        if os.name == "nt":
            self.skipTest("posix launcher test")
        dirs, _ = self.build()
        root = dirs[-1]
        with tempfile.TemporaryDirectory() as tmp:
            modules = Path(tmp) / "node_modules"
            node_os = "darwin" if sys.platform == "darwin" else "linux"
            cpu = run(["node", "-p", "process.arch"]).stdout.strip()
            matching = [d for d in dirs[:-1] if d.name.endswith(f"-{node_os}-{cpu}")]
            if not matching:
                self.skipTest(f"no platform package for {node_os}-{cpu}")
            installed = modules / "@craftainer" / matching[0].name
            installed.parent.mkdir(parents=True)
            os.rename(matching[0], installed)
            os.rename(root, modules / "template-rust-cli")
            launcher = modules / "template-rust-cli" / "bin" / "template-rust-cli.js"
            result = run(["node", str(launcher), "--version"])
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "template-rust-cli 1.2.3"))
            os.rename(installed, installed.with_name("gone"))
            missing = run(["node", str(launcher)])
            self.assertEqual(missing.returncode, 1)
            self.assertIn("not installed", missing.stderr)


class Homebrew(unittest.TestCase):
    def test_formula(self):
        with tempfile.TemporaryDirectory() as tmp:
            made = make_all_archives(Path(tmp))
            text = rb.build_formula("1.2.3", Path(tmp), rb.load_project())
        slug = rb.github_slug(rb.load_project()["repository"])
        self.assertTrue(text.startswith("class TemplateRustCli < Formula\n"))
        self.assertIn('version "1.2.3"', text)
        self.assertIn('license "GPL-3.0-only"', text)
        for target in ("aarch64-apple-darwin", "x86_64-apple-darwin", "aarch64-unknown-linux-musl", "x86_64-unknown-linux-musl"):
            self.assertIn(
                f'url "https://github.com/{slug}/releases/download/v1.2.3/template-rust-cli-{target}.tar.gz"\n'
                f'      sha256 "{made[target][1]}"',
                text,
            )
        self.assertNotIn("windows", text)
        self.assertIn('bin.install "template-rust-cli"', text)
        self.assertIn('shell_output("#{bin}/template-rust-cli --version")', text)
        # every `do` block, plus `def install` and the class, closes with an `end`
        self.assertEqual(text.count(" do\n") + 2, text.count("end\n"))

    def test_class_names(self):
        self.assertEqual(rb.formula_class("modfence"), "Modfence")
        self.assertEqual(rb.formula_class("cli-mod_fence"), "CliModFence")

    def test_descriptions_are_escaped(self):
        self.assertEqual(rb.ruby_string('a "b" #{c}'), '"a \\"b\\" \\#{c}"')

    def test_a_missing_checksum_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(FileNotFoundError):
            rb.build_formula("1.0.0", Path(tmp), rb.load_project())


class StageImage(unittest.TestCase):
    def test_only_the_linux_binaries_are_staged(self):
        with tempfile.TemporaryDirectory() as tmp:
            archives, out = Path(tmp) / "a", Path(tmp) / "out"
            archives.mkdir()
            make_all_archives(archives)
            staged = rb.stage_image(archives, out, rb.load_project())
            self.assertEqual(sorted(p.relative_to(out).as_posix() for p in staged), ["linux-amd64/cli", "linux-arm64/cli"])
            for path in staged:
                self.assertEqual(path.read_bytes(), FAKE_BINARY)
                self.assertTrue(path.stat().st_mode & stat.S_IXUSR)

    def test_dockerfile_matches_the_context_layout(self):
        text = (BIN / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY --chmod=0555 linux-${TARGETARCH}/cli /cli", text)
        self.assertIn('ENTRYPOINT ["/cli"]', text)
        self.assertIn("USER 65532:65532", text)


class PublishNpm(unittest.TestCase):
    """publish-npm.sh against a fake `npm` that records what it was asked."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.log = self.tmp / "npm.log"
        fake = self.tmp / "bin"
        fake.mkdir()
        npm = fake / "npm"
        npm.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = --version ]; then echo "${FAKE_NPM_VERSION:-11.19.1}"; exit 0; fi\n'
            'echo "$*" >> "$FAKE_LOG"\n'
            'if [ "$1" = install ]; then mkdir -p "$3/node_modules/.bin"; cp "$0" "$3/node_modules/.bin/npm"; exit 0; fi\n'
            'if [ "$1" = view ]; then\n'
            '  spec=$3\n'
            '  case " $FAKE_PUBLISHED " in *" $spec "*) echo "\\"1\\""; exit 0;; esac\n'
            '  if [ "$3" = "$FAKE_LATEST_OF" ]; then echo "\\"$FAKE_LATEST\\""; exit 0; fi\n'
            '  echo \'{"error":{"code":"E404"}}\'; exit 1\n'
            "fi\n"
            "exit 0\n"
        )
        npm.chmod(0o755)
        self.path = f"{fake}{os.pathsep}{os.environ['PATH']}"
        self.platform_dir, self.root_dir = self.tmp / "platform", self.tmp / "root"
        self.platform_dir.mkdir()
        self.root_dir.mkdir()

    def tarball(self, directory, name, version, repo="git+https://github.com/o/r.git"):
        path = directory / (name.replace("/", "-").lstrip("@") + f"-{version}.tgz")
        body = json.dumps({"name": name, "version": version, "repository": {"type": "git", "url": repo}}).encode()
        with tarfile.open(path, "w:gz") as tf:
            info = tarfile.TarInfo("package/package.json")
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
        return path

    def publish(self, env=None):
        full = {"PATH": self.path, "FAKE_LOG": str(self.log), "GITHUB_REPOSITORY": "o/r", "FAKE_PUBLISHED": "", **(env or {})}
        for key in ("NPM_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL"):
            full.setdefault(key, "")
        return run(["scripts/binaries/publish-npm.sh", str(self.platform_dir), str(self.root_dir)], env=full)

    def calls(self, verb):
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [line for line in lines if line.startswith(verb)]

    def two_packages(self, version="1.2.3"):
        self.tarball(self.platform_dir, "@o/cli-linux-x64", version)
        self.tarball(self.root_dir, "cli", version)

    def test_publishes_platform_packages_before_the_root_with_provenance(self):
        self.two_packages()
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        published = self.calls("publish")
        self.assertEqual(len(published), 2)
        self.assertIn("cli-linux-x64-1.2.3.tgz", published[0].replace("@o-", ""))
        self.assertTrue(published[1].split()[1].endswith("cli-1.2.3.tgz"))
        for line in published:
            self.assertIn("--provenance --access public --tag latest", line)

    def test_a_token_stages_instead_of_publishing(self):
        self.two_packages()
        r = self.publish({"NPM_TOKEN": "tok-123"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls("stage publish")), 2)
        self.assertEqual(self.calls("publish"), [])
        self.assertNotIn("tok-123", self.log.read_text(), "the token must not reach npm's argv")
        self.assertNotIn("tok-123", r.stdout + r.stderr)

    def test_an_already_published_version_is_skipped(self):
        self.two_packages()
        r = self.publish({"FAKE_PUBLISHED": "@o/cli-linux-x64@1.2.3"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already on the registry", r.stdout)
        self.assertEqual(len(self.calls("publish")), 1)

    def test_a_repository_mismatch_stops_before_any_upload(self):
        self.tarball(self.platform_dir, "@o/cli-linux-x64", "1.2.3")
        self.tarball(self.root_dir, "cli", "1.2.3", repo="git+https://github.com/other/repo.git")
        r = self.publish()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("repository.url", r.stderr)
        self.assertEqual(self.calls("publish"), [])

    def test_prerelease_gets_its_identifier_as_the_tag(self):
        self.two_packages("1.2.3-rc.1")
        self.assertEqual(self.publish().returncode, 0)
        for line in self.calls("publish"):
            self.assertTrue(line.endswith("--tag rc"), line)

    def test_a_backport_does_not_take_latest(self):
        self.two_packages("1.4.3")
        r = self.publish({"FAKE_LATEST_OF": "cli", "FAKE_LATEST": "2.0.0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        published = self.calls("publish")
        self.assertTrue(published[-1].endswith("--tag v1-latest"), published)

    def test_an_older_npm_is_replaced_by_the_pinned_one(self):
        self.two_packages()
        r = self.publish({"FAKE_NPM_VERSION": "10.9.0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls("install --prefix")), 1)
        self.assertIn("npm@11.19.1", self.calls("install --prefix")[0])
        self.assertEqual(len(self.calls("publish")), 2)

    def test_the_pinned_npm_is_used_as_is(self):
        self.two_packages()
        self.assertEqual(self.publish().returncode, 0)
        self.assertEqual(self.calls("install"), [])

    def test_empty_directories_are_an_error(self):
        r = self.publish()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no tarballs", r.stderr)


if __name__ == "__main__":
    unittest.main()
