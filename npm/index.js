#!/usr/bin/env node
// Node-first launcher for the PlaceRoot MCP server. The server itself is
// Python, distributed on PyPI; this just spawns `uvx placeroot`, passing
// through argv, stdio, and exit code, so `npx placeroot` behaves the same
// as `uvx placeroot` (including flags like --http). No dependencies.

"use strict";

const { spawn } = require("child_process");

const args = process.argv.slice(2);

const child = spawn("uvx", ["placeroot", ...args], { stdio: "inherit" });

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
    console.error(`PlaceRoot: failed to launch \`uvx placeroot\`: ${err.message}`);
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
