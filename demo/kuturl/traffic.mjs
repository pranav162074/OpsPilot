// Steady, realistic background traffic for the demo: mostly redirects, some API reads.
// Without it Prometheus would have nothing to show and no alert could fire.
// Creates its own API key and links on first start and keeps them in /state.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';

const BASE = process.env.KUTURL_URL ?? 'http://kuturl:3000';
const RATE = Number(process.env.TRAFFIC_RPS ?? 8);
const STATE = '/state/traffic.json';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForKuturl() {
  for (;;) {
    try {
      if ((await fetch(`${BASE}/ping`)).ok) return;
    } catch {
      // not up yet
    }
    await sleep(2000);
  }
}

async function setup() {
  try {
    return JSON.parse(readFileSync(STATE, 'utf8'));
  } catch {
    // first start
  }
  const keyRes = await fetch(`${BASE}/api/v1/keys`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: 'demo-traffic' }),
  });
  if (!keyRes.ok) throw new Error(`Couldn't create a Kuturl API key: ${keyRes.status}`);
  const { apiKey: key } = await keyRes.json();
  const links = Array.from({ length: 25 }, (_, i) => ({
    url: `https://example.com/products/${i}?ref=demo`,
  }));
  const bulk = await fetch(`${BASE}/api/v1/links/bulk`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}` },
    body: JSON.stringify({ links }),
  });
  if (!bulk.ok) throw new Error(`Couldn't create links: ${bulk.status} ${await bulk.text()}`);
  const codes = (await bulk.json()).results.filter((r) => r.code).map((r) => r.code);
  const state = { key, codes };
  mkdirSync('/state', { recursive: true });
  writeFileSync(STATE, JSON.stringify(state));
  return state;
}

async function hit(url, init) {
  try {
    const res = await fetch(url, { redirect: 'manual', signal: AbortSignal.timeout(10_000), ...init });
    await res.arrayBuffer();
  } catch {
    // failures are the point during an incident; Kuturl's metrics record them
  }
}

await waitForKuturl();
const { key, codes } = await setup();
console.log(`traffic: ${codes.length} links, about ${RATE} requests a second`);
for (let i = 0; ; i += 1) {
  const code = codes[Math.floor(Math.random() * codes.length)];
  if (i % 10 === 0) {
    // API reads always go to Postgres, so they show database trouble straight away
    void hit(`${BASE}/api/v1/links?limit=10`, { headers: { Authorization: `Bearer ${key}` } });
  } else if (i % 25 === 0) {
    void hit(`${BASE}/zzzzzzz`); // a code that doesn't exist: a normal trickle of 404s
  } else {
    void hit(`${BASE}/${code}`);
  }
  await sleep(1000 / RATE);
}
