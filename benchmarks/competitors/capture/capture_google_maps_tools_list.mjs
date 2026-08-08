// Capture the archived Anthropic Google Maps reference server's tool list.
//
// Run by hand when refreshing the snapshot; never run by the test suite.
// Writes ../google-maps-archived/tools_list.json.
//
//   git clone https://github.com/modelcontextprotocol/servers-archived.git
//   GOOGLE_MAPS_MCP_SRC=./servers-archived/src/google-maps \
//     node benchmarks/competitors/capture/capture_google_maps_tools_list.mjs
//
// That server refuses to start without GOOGLE_MAPS_API_KEY (it calls
// process.exit(1) at module load, before any handler is registered), so the
// tool list cannot be read over stdio the way Mapbox's can. Instead the
// literal tool-definition region of their `index.ts` — everything between the
// `// Tool definitions` and `// API handlers` comments, which is pure object
// literals — is isolated and evaluated. The two TypeScript-only bits in that
// region (`: Tool` annotations and the trailing `as const`) are stripped. No
// figure is transcribed by hand.

import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, '..', 'google-maps-archived', 'tools_list.json');
const SRC_DIR = resolve(process.env.GOOGLE_MAPS_MCP_SRC ?? '../servers-archived/src/google-maps');

const source = readFileSync(join(SRC_DIR, 'index.ts'), 'utf8');
const start = source.indexOf('// Tool definitions');
const end = source.indexOf('// API handlers');
if (start < 0 || end < 0) {
  throw new Error('tool-definition region markers not found in index.ts');
}
const region = source
  .slice(start, end)
  .replace(/: Tool\b/g, '')
  .replace(/\] as const;/, '];');

const tools = new Function(region + '\nreturn MAPS_TOOLS;')();
writeFileSync(OUT, JSON.stringify({ tools }, null, 2) + '\n');
console.error(`wrote ${OUT} (${tools.length} tools)`);
