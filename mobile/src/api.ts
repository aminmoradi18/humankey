// Talks to web/server.py. 10.0.2.2 is the laptop as seen from the Android emulator.
import { Platform } from 'react-native';

import { getItem, setItem } from './store';

export type Finding = { check: string; signal: string; summary: string };
export type Item = {
  invoice_id: string; vendor_id: string; amount: number; risk: string; tier: 'phone' | 'hardware';
  key_text: string; explanation: string; last_paid_match: boolean | null;
  ai?: { models?: string[]; calls?: number }; subject: string; findings: Finding[]; done: string | null;
};
export type Queue = { items: Item[]; pending: (import('./key').Pending & { invoice_id: string }) | null; busy: string | null };

const SERVER = 'humankey.server';
export const DEFAULT_SERVER = Platform.OS === 'web' ? 'http://127.0.0.1:8700' : 'http://10.0.2.2:8700';

export const getServer = async () => (await getItem(SERVER)) || DEFAULT_SERVER;
export const setServer = (url: string) => setItem(SERVER, url.replace(/\/+$/, ''));

async function call<T>(path: string, body?: object): Promise<T> {
  const res = await fetch((await getServer()) + path, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  } : undefined);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data as T;
}

export const queue = () => call<Queue>('/api/phone/queue');
export type Device = { id: string; kind: 'key' | 'phone' | 'emulator'; counter: number; active: boolean };
export type Devices = { device_kind: string; devices: Device[]; paid: Record<string, number>; limits: { auto: number; phone: number } };
export const devices = () => call<Devices>('/api/phone/devices');
export const pair = (code: string, id: string, pub: string) => call('/api/phone/pair', { code, id, pub });
export const release = (invoice_id: string) => call('/api/phone/release', { invoice_id });
export const decide = (msg: object) => call('/api/phone/decision', msg);
