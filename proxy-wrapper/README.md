# proxy-wrapper (archived)

A small Node.js shim that wraps [`@rlabs-inc/gemini-mcp`](https://www.npmjs.com/package/@rlabs-inc/gemini-mcp) so the underlying HTTP client honours `HTTPS_PROXY` / `HTTP_PROXY`. Uses `undici`'s `ProxyAgent` as a global dispatcher before dynamically importing the upstream server.

**Status: archived.** Kept for reference; the Python server at the repository root is the recommended solution because:

- it works around several Windows-specific issues that this Node wrapper does not (cmd.exe argument escaping, `ProactorBasePipeTransport` errors, stdin inheritance)
- it falls back to the Google Generative AI API when the CLI fails
- it implements path-traversal and credential-filename guards before forwarding code to the model

## What this wrapper does

```js
import { ProxyAgent, setGlobalDispatcher } from 'undici';

const proxyUrl = process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
if (proxyUrl) {
  setGlobalDispatcher(new ProxyAgent(proxyUrl));
}

// then dynamic-imports @rlabs-inc/gemini-mcp from the global npm location
```

## Known limitations

- The path to `@rlabs-inc/gemini-mcp` is hardcoded to the Windows global npm location (`%APPDATA%\npm\node_modules\…`); change `gemini-proxy-wrapper.mjs` if your prefix is elsewhere.
- No error handling around the dynamic import — if the upstream package is missing you get a raw module-not-found.
- Not maintained. File issues against the Python server instead.
