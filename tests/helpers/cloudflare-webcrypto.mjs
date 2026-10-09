// Cloudflare provides Web Crypto; Node 18 ESM does not expose it by default.
// This adapter is only imported by offline tests, never by the Worker.
import {webcrypto} from 'node:crypto';
if (globalThis.crypto === undefined) globalThis.crypto = webcrypto;
