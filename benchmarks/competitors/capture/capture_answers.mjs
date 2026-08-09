// Capture what the competitor MCP servers actually return, per scenario.
//
// Run by hand when refreshing the snapshots; never run by the test suite.
// Writes ../answers.json.
//
// Method, stated plainly because it is the load-bearing part of the
// comparison: their real servers are run, unmodified except as noted below,
// over stdio. Their upstream HTTP calls are pointed at a local stub that
// replies with the vendor's own *documented example response* for that
// endpoint (see ../upstream_examples/, produced by extract_doc_examples.py).
// So every competitor answer here is their code's real output — their
// formatting, their field selection, their added hints — for an input payload
// they themselves publish. No API keys, no live traffic, no hand-typed JSON.
//
// Two mechanical redirections are needed:
//   - Mapbox reads its API base from MAPBOX_API_ENDPOINT (src/tools/
//     MapboxApiBasedTool.ts), so it is redirected with an env var only.
//   - The archived Google server hardcodes https://maps.googleapis.com, so its
//     *built* dist/index.js is sed-patched to the stub host. Nothing else is
//     changed; the patch is applied by this script and reported below.
//
//   MAPBOX_MCP_DIR=./mcp-server GOOGLE_MAPS_MCP_SRC=./servers-archived/src/google-maps \
//     node benchmarks/competitors/capture/capture_answers.mjs

import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const EXAMPLES = join(HERE, '..', 'upstream_examples');
const OUT = join(HERE, '..', 'answers.json');
const MAPBOX_DIR = resolve(process.env.MAPBOX_MCP_DIR ?? '../mapbox-mcp');
const GOOGLE_DIR = resolve(process.env.GOOGLE_MAPS_MCP_SRC ?? '../servers-archived/src/google-maps');
const STUB_PORT = 8799;
const STUB_ORIGIN = `http://127.0.0.1:${STUB_PORT}`;

// upstream endpoint path -> which documented example answers it
const ROUTES = [
  [/^\/search\/geocode\/v6\/reverse/, 'mb_forward_geocode'],
  [/^\/search\/geocode\/v6\/forward/, 'mb_forward_geocode'],
  [/^\/search\/searchbox\/v1\/category/, 'mb_searchbox_category'],
  [/^\/search\/searchbox\/v1\/forward/, 'mb_searchbox_forward'],
  [/^\/directions-matrix\/v1/, 'mb_matrix'],
  [/^\/directions\/v5/, 'mb_directions'],
  [/^\/isochrone\/v1/, 'mb_isochrone'],
  [/^\/maps\/api\/geocode\/json/, 'g_geocode']
];

const unmatched = new Set();
const stub = createServer((req, res) => {
  const path = req.url.split('?')[0];
  const hit = ROUTES.find(([pattern]) => pattern.test(path));
  if (!hit) {
    unmatched.add(path);
    res.writeHead(404, { 'content-type': 'application/json' });
    res.end('{}');
    return;
  }
  res.writeHead(200, { 'content-type': 'application/json' });
  res.end(readFileSync(join(EXAMPLES, `${hit[1]}.json`), 'utf8'));
});
await new Promise((ready) => stub.listen(STUB_PORT, '127.0.0.1', ready));

function stdioClient(args, env, cwd) {
  const child = spawn('node', args, { cwd, env: { ...process.env, ...env }, stdio: ['pipe', 'pipe', 'pipe'] });
  child.stderr.resume();
  let buffer = '';
  let nextId = 0;
  const pending = new Map();
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
      if (message.id != null && pending.has(message.id)) {
        pending.get(message.id)(message);
        pending.delete(message.id);
      }
    }
  });
  const call = (method, params) =>
    new Promise((resolve_, reject) => {
      const id = ++nextId;
      pending.set(id, resolve_);
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
      setTimeout(() => reject(new Error(`timed out: ${method} ${JSON.stringify(params)}`)), 25000);
    });
  return {
    call,
    async init() {
      await call('initialize', {
        protocolVersion: '2025-06-18',
        capabilities: {},
        clientInfo: { name: 'placeroot-benchmark-capture', version: '1' }
      });
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
    },
    kill: () => child.kill()
  };
}

const answers = [];
async function record(client, server, scenario, tool, args, upstreamExample) {
  const reply = await client.call('tools/call', { name: tool, arguments: args });
  const payload = reply.result ?? reply.error;
  const text =
    payload?.content?.map((block) => (block.type === 'text' ? block.text : '')).join('\n') ??
    JSON.stringify(payload);
  answers.push({
    server,
    scenario,
    tool,
    arguments: args,
    upstream_example: `benchmarks/competitors/upstream_examples/${upstreamExample}.json`,
    is_error: Boolean(payload?.isError) || Boolean(reply.error),
    response_text: text
  });
  console.error(`${server}/${scenario}: ${text.length} chars`);
}

// ---------------------------------------------------------------- Mapbox ----
const mapbox = stdioClient(
  ['dist/esm/index.js'],
  {
    MAPBOX_ACCESS_TOKEN: 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.signature',
    MAPBOX_API_ENDPOINT: `${STUB_ORIGIN}/`
  },
  MAPBOX_DIR
);
await mapbox.init();
await record(mapbox, 'mapbox-mcp', 'geocode_address', 'search_and_geocode_tool', { q: '2 Lincoln Memorial Circle NW, Washington, DC' }, 'mb_searchbox_forward');
await record(mapbox, 'mapbox-mcp', 'reverse_geocode', 'reverse_geocode_tool', { longitude: -77.05, latitude: 38.89 }, 'mb_forward_geocode');
await record(mapbox, 'mapbox-mcp', 'nearest_coffee', 'category_search_tool', { category: 'coffee', proximity: { longitude: -77.05, latitude: 38.89 }, limit: 10 }, 'mb_searchbox_category');
await record(mapbox, 'mapbox-mcp', 'route_a_to_b', 'directions_tool', { coordinates: [{ longitude: -77.05, latitude: 38.89 }, { longitude: -77.03, latitude: 38.9 }], routing_profile: 'mapbox/driving' }, 'mb_directions');
await record(mapbox, 'mapbox-mcp', 'isochrone_15min', 'isochrone_tool', { coordinates: { longitude: -77.05, latitude: 38.89 }, profile: 'mapbox/walking', contours_minutes: [15], generalize: 1000 }, 'mb_isochrone');
await record(mapbox, 'mapbox-mcp', 'matrix_3x3', 'matrix_tool', { coordinates: [{ longitude: -77.05, latitude: 38.89 }, { longitude: -77.03, latitude: 38.9 }, { longitude: -77.01, latitude: 38.91 }], profile: 'mapbox/driving' }, 'mb_matrix');
mapbox.kill();

// ------------------------------------------------- Google Maps (archived) ----
// Redirect the hardcoded API host in the built server to the stub.
const builtPath = join(GOOGLE_DIR, 'dist', 'index.js');
const built = readFileSync(builtPath, 'utf8');
if (built.includes('https://maps.googleapis.com')) {
  writeFileSync(builtPath, built.split('https://maps.googleapis.com').join(STUB_ORIGIN));
  console.error(`patched ${builtPath}: https://maps.googleapis.com -> ${STUB_ORIGIN}`);
}

const google = stdioClient(['dist/index.js'], { GOOGLE_MAPS_API_KEY: 'unused-stub-endpoint' }, GOOGLE_DIR);
await google.init();
await record(google, 'google-maps-archived', 'geocode_address', 'maps_geocode', { address: '1600 Amphitheatre Parkway, Mountain View, CA' }, 'g_geocode');
await record(google, 'google-maps-archived', 'reverse_geocode', 'maps_reverse_geocode', { latitude: 37.4224764, longitude: -122.0842499 }, 'g_geocode');
google.kill();

writeFileSync(OUT, JSON.stringify(answers, null, 2) + '\n');
console.error(`wrote ${OUT}`);
if (unmatched.size) console.error('unmatched stub paths:', [...unmatched]);
stub.close();
process.exit(0);
