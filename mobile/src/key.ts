// The phone's own Ed25519 key. The seed never leaves SecureStore; the gate only ever sees the
// public key (at pairing) and signatures. The message layout matches gate/crypto.py decision_message.
import * as Crypto from 'expo-crypto';
import { getItem, setItem } from './store';
import nacl from 'tweetnacl';

const SEED = 'humankey.seed';
const ID = 'humankey.id';
const COUNTER = 'humankey.counter';

const b64 = (b: Uint8Array) => {
  let s = '';
  b.forEach((x) => (s += String.fromCharCode(x)));
  return globalThis.btoa(s);
};
const unb64 = (s: string) => Uint8Array.from(globalThis.atob(s), (c) => c.charCodeAt(0));
const hex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');

export type Identity = { id: string; pub: string };

async function keyPair() {
  let seed = await getItem(SEED);
  if (!seed) {
    seed = b64(await Crypto.getRandomBytesAsync(32));
    await setItem(SEED, seed);
    await setItem(ID, 'phone-' + hex(await Crypto.getRandomBytesAsync(3)));
  }
  return nacl.sign.keyPair.fromSeed(unb64(seed));
}

export async function identity(): Promise<Identity> {
  const kp = await keyPair();
  return { id: (await getItem(ID))!, pub: b64(kp.publicKey) };
}

export const sha256 = (text: string) => Crypto.digestStringAsync(Crypto.CryptoDigestAlgorithm.SHA256, text);

export type Pending = { req: string; text: string; c: string; cj: string; exp: number; cov: number };

// Clear signing, like the hardware key: hash the contract bytes here, read the invoice and amount
// from those bytes, and refuse if they are not what this screen shows. Returns the hash to sign.
export async function checkContract(p: Pending, invoice: string, amount: number): Promise<string> {
  const c = await sha256(p.cj);
  const k = JSON.parse(p.cj);
  const pay = k.intent?.parameters ?? {};
  if (k.contractId !== p.req) throw new Error('contract is for another request; not signing');
  if (pay.invoice_id !== invoice || Math.abs(Number(pay.amount) - amount) > 0.005)
    throw new Error(`contract says ${pay.invoice_id} $${Number(pay.amount).toLocaleString('en-US')}, screen says ${invoice}; not signing`);
  if (k.evidence?.displaySha256 && k.evidence.displaySha256 !== (await sha256(p.text)))
    throw new Error('contract text differs from what is shown; not signing');
  return c;
}

// Sign what the screen showed. The counter only goes up, so an old signature can't be replayed.
export async function signDecision(p: Pending, d: 'approve' | 'reject') {
  const kp = await keyPair();
  const counter = Number((await getItem(COUNTER)) || '0') + 1;
  await setItem(COUNTER, String(counter));
  const th = await sha256(p.text);
  const msg = `${p.req}|${p.c}|${th}|${d}|${counter}|${p.exp}|${p.cov}|none:`;
  const sig = nacl.sign.detached(new TextEncoder().encode(msg), kp.secretKey);
  return { req: p.req, d, counter, th, sig: b64(sig), exp: p.exp, cov: p.cov, id: (await identity()).id };
}
