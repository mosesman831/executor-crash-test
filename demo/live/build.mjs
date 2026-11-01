import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = dirname(fileURLToPath(import.meta.url));
export async function buildWorker() {
  let source = await readFile(resolve(dir, 'worker.mjs'), 'utf8');
  for (const [marker, path] of [
    ['__LIVE_PAGE__', 'index.html'],
    ['__RECORDING_PAGE__', '../index.html'],
    ['__TRACE_JSON__', '../trace.json'],
    ['__EVENTS_JSON__', '../events.json'],
  ]) {
    const text = await readFile(resolve(dir, path), 'utf8');
    source = source.replace(marker, () => JSON.stringify(text));
  }
  return source;
}
