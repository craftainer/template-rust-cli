#!/usr/bin/env node
// The root npm package's launcher: finds the platform package npm installed
// for this machine (one of the root package.json's optionalDependencies,
// selected by npm's own `os`/`cpu` fields) and runs its binary with the same
// arguments, streams and exit status. No download happens at install or run
// time, so it works offline and behind a registry mirror.
"use strict";

const { spawnSync } = require("node:child_process");
const path = require("node:path");
const pkg = require("../package.json");

const cli = Object.keys(pkg.bin)[0];

function fail(message) {
  console.error(`${cli}: ${message}`);
  process.exit(1);
}

// Platform packages are named `<scope>/<cli>-<process.platform>-<process.arch>`.
const suffix = `-${process.platform}-${process.arch}`;
const name = Object.keys(pkg.optionalDependencies || {}).find((dep) => dep.endsWith(suffix));
if (!name) {
  fail(`no prebuilt binary for ${process.platform}/${process.arch}`);
}

let directory;
try {
  directory = path.dirname(require.resolve(`${name}/package.json`));
} catch {
  fail(
    `the platform package ${name} is not installed; reinstall without ` +
      "--omit=optional / --no-optional",
  );
}

const exe = path.join(directory, "bin", cli + (process.platform === "win32" ? ".exe" : ""));
const result = spawnSync(exe, process.argv.slice(2), { stdio: "inherit" });
if (result.error) {
  fail(result.error.message);
}
if (result.signal) {
  process.kill(process.pid, result.signal);
}
process.exit(result.status ?? 1);
