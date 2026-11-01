// Local preview only. The server listens on loopback and injects a local owner identity.
import http from 'node:http';
import { Readable } from 'node:stream';
import { buildWorker } from './build.mjs';

if (!process.env.OPENAI_API_KEY) {
  console.error('OPENAI_API_KEY is missing. Run from the repository root with --env-file=.env.');
  process.exit(1);
}
const source = await buildWorker();
const worker = (await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))).default;
const server = http.createServer(async (incoming, outgoing) => {
  const port = server.address().port;
  if (incoming.headers.host !== `127.0.0.1:${port}`) { outgoing.writeHead(403); outgoing.end(); return; }
  try {
    const headers = new Headers(incoming.headers);
    headers.set('oai-authenticated-user-id', 'local-owner');
    const options = { method: incoming.method, headers };
    if (!['GET', 'HEAD'].includes(incoming.method)) {
      options.body = Readable.toWeb(incoming);
      options.duplex = 'half';
    }
    const request = new Request(`http://127.0.0.1:${port}${incoming.url}`, options);
    const response = await worker.fetch(request, { OPENAI_API_KEY: process.env.OPENAI_API_KEY });
    outgoing.writeHead(response.status, Object.fromEntries(response.headers));
    if (response.body) Readable.fromWeb(response.body).pipe(outgoing);
    else outgoing.end();
  } catch {
    if (!outgoing.headersSent) outgoing.writeHead(500, { 'Content-Type': 'text/plain' });
    outgoing.end('The local demo request failed.');
  }
});
server.listen(0, '127.0.0.1', () => console.log(`Live demo: http://127.0.0.1:${server.address().port}/`));
