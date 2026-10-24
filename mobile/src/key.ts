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

export type Pending = { req: string; text: string; c: string; exp: number; cov: number };

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
