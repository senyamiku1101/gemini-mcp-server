import { ProxyAgent, setGlobalDispatcher } from 'undici';

const proxyUrl = process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
if (proxyUrl) {
  setGlobalDispatcher(new ProxyAgent(proxyUrl));
}

import { createRequire } from 'module';
import { resolve } from 'path';
import { homedir } from 'os';

const geminiMcpPath = resolve(
  homedir(), 'AppData', 'Roaming', 'npm',
  'node_modules', '@rlabs-inc', 'gemini-mcp', 'dist', 'server.js'
);
await import(`file://${geminiMcpPath.replace(/\\/g, '/')}`);
