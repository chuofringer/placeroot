#!/usr/bin/env node
// Node-first launcher for the PlaceRoot MCP server. The server itself is
// Python, distributed on PyPI; this just spawns `uvx placeroot==<version>`,
// passing through argv, stdio, and exit code, so `npx placeroot` behaves the
// same as `uvx placeroot` (including flags like --http). No dependencies.
//
// The PyPI version is pinned to this package's own version (the two are
// published as a pair by the release workflow), so `npx placeroot@0.9.0`
// runs placeroot 0.9.0 rather than whatever PyPI's latest happens to be.

"use strict";

const { spawn } = require("child_process");
const { version } = require("./package.json");

const args = process.argv.slice(2);
const spec = `placeroot==${version}`;

const child = spawn("uvx", [spec, ...args], { stdio: "inherit" });

child.on("error", (err) => {
  if (err.code === "ENOENT") {
    console.error(
      [
        "PlaceRoot: could not find `uvx` on your PATH.",
        "",
        "PlaceRoot's MCP server is written in Python and distributed via uv/PyPI.",
        "Install uv, then re-run this command:",
        "",
        "  - See https://docs.astral.sh/uv/ for install instructions, or",
        "  - pip install uv",
        "",
        "Once uv is installed, `npx placeroot` will work the same as `uvx placeroot`.",
      ].join("\n")
    );
    process.exit(1);
  } else {
    console.error(`PlaceRoot: failed to launch \`uvx ${spec}\`: ${err.message}`);
    process.exit(1);
  }
});

child.on("exit", (code, signal) => {
  if (signal) {
    // Re-raise the same signal so shells / process managers see the
    // conventional 128+signal-style termination.
    process.kill(process.pid, signal);
  } else {
    process.exit(code === null ? 1 : code);
  }
});
