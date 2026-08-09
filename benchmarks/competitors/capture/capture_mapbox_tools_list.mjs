// Capture Mapbox MCP's real `tools/list` reply.
//
// Network-using capture step (npm install of their repo). Run by hand when
// refreshing the snapshot; never run by the test suite. Writes
// ../mapbox-mcp/tools_list.json.
//
//   git clone https://github.com/mapbox/mcp-server.git && (cd mcp-server && npm ci && npm run build)
//   MAPBOX_MCP_DIR=./mcp-server node benchmarks/competitors/capture/capture_mapbox_tools_list.mjs
//
// The access token below is a syntactically valid dummy: their server refuses
// to start without one, but `tools/list` never reaches the Mapbox API, so no
// key and no network call is involved in producing the schema surface.

import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, '..', 'mapbox-mcp', 'tools_list.json');
const SERVER_DIR = resolve(process.env.MAPBOX_MCP_DIR ?? '../mapbox-mcp');
const DUMMY_TOKEN = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.signature';

const child = spawn('node', ['dist/esm/index.js'], {
  cwd: SERVER_DIR,
  env: { ...process.env, MAPBOX_ACCESS_TOKEN: DUMMY_TOKEN },
  stdio: ['pipe', 'pipe', 'inherit']
});

let buffer = '';
child.stdout.on('data', (chunk) => {
  buffer += chunk.toString();
  let newline;
  while ((newline = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    if (!line.trim()) continue;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      continue;
    }
    if (message.id === 1) {
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list' }) + '\n');
    } else if (message.id === 2) {
      writeFileSync(OUT, JSON.stringify(message.result, null, 2) + '\n');
      console.error(`wrote ${OUT} (${message.result.tools.length} tools)`);
      child.kill();
      process.exit(0);
    }
  }
});

child.stdin.write(
  JSON.stringify({
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize',
    params: {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'placeroot-benchmark-capture', version: '1' }
    }
  }) + '\n'
);

setTimeout(() => {
  console.error('timed out waiting for tools/list');
  process.exit(1);
}, 30000);
