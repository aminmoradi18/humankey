// HumanKey Approve: the low-value tier. The phone may approve only what the gate marks as
// "phone" (under $10,000 and no check raised risk). Everything else shows here read-only and
// must be approved on the hardware key.
import * as Haptics from 'expo-haptics';
import { LinearGradient } from 'expo-linear-gradient';
import * as LocalAuthentication from 'expo-local-authentication';
import { getItem, setItem } from './src/store';
import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator, Platform, Pressable, RefreshControl, SafeAreaView, ScrollView, StyleSheet, Text, TextInput, View,
} from 'react-native';

import { DEFAULT_SERVER, Item, Queue, decide, getServer, pair, queue, release, setServer } from './src/api';
import { Identity, identity, signDecision } from './src/key';

const C = {
  bg: ['#070A12', '#0D1324', '#111A33'] as const, card: 'rgba(255,255,255,0.06)', line: 'rgba(255,255,255,0.10)',
  text: '#F2F5FF', dim: '#8E99B8', green: '#3DDC97', amber: '#FFB547', red: '#FF5C7A', blue: '#6C8CFF',
};
const money = (n: number) => '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export default function App() {
  const [me, setMe] = useState<Identity | null>(null);
  const [paired, setPaired] = useState<boolean | null>(null);
  const [q, setQ] = useState<Queue | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    (async () => {
      setMe(await identity());
      setPaired((await getItem('humankey.paired')) === 'yes');
    })();
  }, []);

  const refresh = useCallback(async () => {
    try {
      setQ(await queue());
      setErr('');
    } catch (e: any) {
      setErr(`Can't reach HumanKey (${e.message})`);
    }
  }, []);

  useEffect(() => {
    if (!paired) return;
    refresh();
    const t = setInterval(refresh, 2500);
    return () => clearInterval(t);
  }, [paired, refresh]);

  const item = q?.items.find((i) => i.invoice_id === open);
  return (
    <LinearGradient colors={C.bg} style={{ flex: 1 }}>
      <StatusBar style="light" />
      <SafeAreaView style={{ flex: 1 }}>
        {paired === null || !me ? <ActivityIndicator color={C.blue} style={{ marginTop: 120 }} />
          : !paired ? <Pairing me={me} onDone={() => setPaired(true)} />
          : item ? <Detail item={item} onBack={() => setOpen(null)} refresh={refresh} />
          : <Home q={q} err={err} me={me} onOpen={setOpen} refresh={refresh} />}
      </SafeAreaView>
    </LinearGradient>
  );
}

function Header({ title, sub }: { title: string; sub?: string }) {
  return (
    <View style={{ paddingHorizontal: 22, paddingTop: 28, paddingBottom: 12 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
        <View style={s.logo}><Text style={{ color: C.bg[0], fontWeight: '900', fontSize: 16 }}>H</Text></View>
        <Text style={{ color: C.dim, fontSize: 13, letterSpacing: 2, fontWeight: '700' }}>HUMANKEY</Text>
      </View>
      <Text style={s.h1}>{title}</Text>
      {sub ? <Text style={{ color: C.dim, fontSize: 15, marginTop: 6, lineHeight: 21 }}>{sub}</Text> : null}
    </View>
  );
}

function Pairing({ me, onDone }: { me: Identity; onDone: () => void }) {
  const [code, setCode] = useState('');
  const [server, setSrv] = useState(DEFAULT_SERVER);
  const [msg, setMsg] = useState('');
  useEffect(() => { getServer().then(setSrv); }, []);
  const go = async () => {
    try {
      await setServer(server);
      await pair(code.trim(), me.id, me.pub);
      await setItem('humankey.paired', 'yes');
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      onDone();
    } catch (e: any) {
      setMsg(e.message);
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error);
    }
  };
  return (
    <ScrollView contentContainerStyle={{ paddingBottom: 40 }} keyboardShouldPersistTaps="handled">
      <Header title="Pair this phone" sub="Enter the 6-digit code that the HumanKey server prints in its terminal." />
      <View style={[s.card, { marginHorizontal: 22 }]}>
        <Text style={s.label}>Server</Text>
        <TextInput value={server} onChangeText={setSrv} autoCapitalize="none" style={s.input} placeholderTextColor={C.dim} />
        <Text style={[s.label, { marginTop: 16 }]}>Pairing code</Text>
        <TextInput value={code} onChangeText={setCode} keyboardType="number-pad" maxLength={6}
          style={[s.input, { fontSize: 30, letterSpacing: 12, textAlign: 'center' }]} placeholder="000000" placeholderTextColor="#3A4560" />
        {msg ? <Text style={{ color: C.red, marginTop: 12 }}>{msg}</Text> : null}
        <Pressable onPress={go} disabled={code.length !== 6} style={({ pressed }) => [s.btn, { backgroundColor: C.blue, opacity: code.length !== 6 ? 0.4 : pressed ? 0.8 : 1 }]}>
          <Text style={s.btnText}>Pair</Text>
        </Pressable>
      </View>
      <View style={[s.card, { marginHorizontal: 22, marginTop: 14 }]}>
        <Text style={s.label}>This phone</Text>
        <Text style={{ color: C.text, fontSize: 16, fontWeight: '700', marginTop: 4 }}>{me.id}</Text>
        <Text style={{ color: C.dim, fontSize: 12, marginTop: 6, fontFamily: 'monospace' }}>{me.pub}</Text>
        <Text style={{ color: C.dim, fontSize: 13, marginTop: 10, lineHeight: 19 }}>
          The private key was made on this phone and never leaves it. The server keeps only the public key above.
        </Text>
      </View>
    </ScrollView>
  );
}

function TierBadge({ tier }: { tier: Item['tier'] }) {
  const phone = tier === 'phone';
  return (
    <View style={[s.badge, { backgroundColor: phone ? 'rgba(61,220,151,0.14)' : 'rgba(255,181,71,0.14)' }]}>
      <Text style={{ color: phone ? C.green : C.amber, fontSize: 12, fontWeight: '800' }}>
        {phone ? 'PHONE OK' : 'HARDWARE KEY'}
      </Text>
    </View>
  );
}

function Row({ it, onPress }: { it: Item; onPress: () => void }) {
  const ups = it.findings.filter((f) => f.signal === 'risk_up');
  return (
    <Pressable onPress={onPress} style={({ pressed }) => [s.card, s.row, pressed && { opacity: 0.75 }]}>
      <View style={{ flex: 1 }}>
        <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
          <Text style={{ color: C.dim, fontSize: 13, fontWeight: '700' }}>{it.invoice_id} · {it.vendor_id}</Text>
          <TierBadge tier={it.tier} />
        </View>
        <Text style={s.amount}>{money(it.amount)}</Text>
        <Text style={{ color: ups.length ? C.amber : C.dim, fontSize: 14 }} numberOfLines={1}>
          {it.done ? `Done: ${it.done}` : ups.length ? ups.map((f) => f.summary).join(' · ')
            : it.amount >= 10000 ? 'Checks passed · over the $10,000 limit' : 'Every check passed'}
        </Text>
      </View>
    </Pressable>
  );
}

function Home({ q, err, me, onOpen, refresh }: { q: Queue | null; err: string; me: Identity; onOpen: (id: string) => void; refresh: () => void }) {
  const [busy, setBusy] = useState(false);
  const items = q?.items || [];
  const mine = items.filter((i) => i.tier === 'phone' && !i.done);
  const key = items.filter((i) => i.tier === 'hardware' && !i.done);
  const done = items.filter((i) => i.done);
  const total = mine.reduce((a, i) => a + i.amount, 0);
  return (
    <ScrollView contentContainerStyle={{ paddingBottom: 40 }}
      refreshControl={<RefreshControl refreshing={busy} tintColor={C.dim} onRefresh={async () => { setBusy(true); await refresh(); setBusy(false); }} />}>
      <Header title="Approvals" sub={`Signed on this phone as ${me.id}`} />
      {err ? <Text style={{ color: C.red, marginHorizontal: 22, marginBottom: 10 }}>{err}</Text> : null}
      <LinearGradient colors={['#1D2B5C', '#16213F']} style={[s.card, { marginHorizontal: 22, borderColor: 'rgba(108,140,255,0.35)' }]}>
        <Text style={s.label}>Waiting for you</Text>
        <Text style={[s.amount, { fontSize: 38 }]}>{money(total)}</Text>
        <Text style={{ color: C.dim, fontSize: 14 }}>{mine.length} on this phone · {key.length} need the hardware key</Text>
      </LinearGradient>
      <Section title="You can approve here" items={mine} onOpen={onOpen} empty="Nothing waiting. Small, clean invoices pay themselves." />
      <Section title="Hardware key only" items={key} onOpen={onOpen}
        note="Over $10,000, or a check found risk. A phone can't approve these: the gate refuses a phone signature on them." />
      {done.length ? <Section title="Done" items={done} onOpen={onOpen} /> : null}
    </ScrollView>
  );
}

function Section({ title, items, onOpen, empty, note }: { title: string; items: Item[]; onOpen: (id: string) => void; empty?: string; note?: string }) {
  return (
    <View style={{ marginTop: 26 }}>
      <Text style={s.h2}>{title}</Text>
      {note ? <Text style={{ color: C.dim, fontSize: 13, marginHorizontal: 22, marginBottom: 10, lineHeight: 18 }}>{note}</Text> : null}
      {items.length === 0 && empty ? <Text style={{ color: C.dim, marginHorizontal: 22 }}>{empty}</Text> : null}
      {items.map((it) => <Row key={it.invoice_id} it={it} onPress={() => onOpen(it.invoice_id)} />)}
    </View>
  );
}

function Detail({ item, onBack, refresh }: { item: Item; onBack: () => void; refresh: () => void }) {
  const [state, setState] = useState('');
  const working = useRef(false);
  const phone = item.tier === 'phone' && !item.done;

  const act = async (d: 'approve' | 'reject') => {
    if (working.current) return;
    working.current = true;
    try {
      if (d === 'approve' && Platform.OS !== 'web') {
        const auth = await LocalAuthentication.authenticateAsync({
          promptMessage: `Approve ${money(item.amount)}`, promptSubtitle: `${item.invoice_id} to ${item.vendor_id}`,
        });
        if (!auth.success) { setState('Not confirmed'); return; }
      }
      setState('Asking the gate for a signed contract…');
      await release(item.invoice_id);
      let p = null;
      for (let i = 0; i < 40 && !p; i++) {
        const q = await queue();
        p = q.pending && q.pending.invoice_id === item.invoice_id ? q.pending : null;
        if (!p) await sleep(300);
      }
      if (!p) throw new Error('the gate did not send a contract');
      // Sign only the exact text this screen showed.
      if (p.text !== item.key_text) throw new Error('the gate sent different text than shown; not signing');
      setState('Signing on this phone…');
      await decide(await signDecision(p, d));
      Haptics.notificationAsync(d === 'approve' ? Haptics.NotificationFeedbackType.Success : Haptics.NotificationFeedbackType.Warning);
      setState(d === 'approve' ? 'Approved. The gate verified the signature and paid.' : 'Rejected. Nothing was paid.');
      await sleep(600);
      refresh();
    } catch (e: any) {
      setState(e.message);
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error);
    } finally {
      working.current = false;
    }
  };

  return (
    <ScrollView contentContainerStyle={{ paddingBottom: 40 }}>
      <Pressable onPress={onBack} style={{ paddingHorizontal: 22, paddingTop: 22 }}>
        <Text style={{ color: C.blue, fontSize: 16, fontWeight: '600' }}>‹ Approvals</Text>
      </Pressable>
      <View style={{ paddingHorizontal: 22, marginTop: 14 }}>
        <TierBadge tier={item.tier} />
        <Text style={[s.amount, { fontSize: 44, marginTop: 10 }]}>{money(item.amount)}</Text>
        <Text style={{ color: C.dim, fontSize: 15 }}>{item.invoice_id} · {item.vendor_id} · risk {item.risk}</Text>
      </View>
      {item.explanation ? (
        <View style={[s.card, { marginHorizontal: 22, marginTop: 18 }]}>
          <Text style={s.label}>Why it's here</Text>
          <Text style={{ color: C.text, fontSize: 15, lineHeight: 22, marginTop: 6 }}>{item.explanation}</Text>
        </View>
      ) : null}
      <Text style={[s.h2, { marginTop: 22 }]}>Checks run through Zetaris</Text>
      <View style={[s.card, { marginHorizontal: 22, paddingVertical: 6 }]}>
        {item.findings.map((f, i) => (
          <View key={f.check} style={[s.check, i > 0 && { borderTopWidth: 1, borderTopColor: C.line }]}>
            <View style={[s.dot, { backgroundColor: f.signal === 'risk_up' ? C.amber : f.signal === 'risk_down' ? C.green : C.dim }]} />
            <View style={{ flex: 1 }}>
              <Text style={{ color: C.dim, fontSize: 12, fontWeight: '700' }}>{f.check.replace(/_/g, ' ').toUpperCase()}</Text>
              <Text style={{ color: C.text, fontSize: 14, marginTop: 2 }}>{f.summary}</Text>
            </View>
          </View>
        ))}
      </View>
      <Text style={[s.h2, { marginTop: 22 }]}>What you sign</Text>
      <View style={[s.card, { marginHorizontal: 22, backgroundColor: '#05070D' }]}>
        <Text style={{ color: C.green, fontFamily: 'monospace', fontSize: 14, lineHeight: 21 }}>{item.key_text}</Text>
      </View>
      {phone ? (
        <View style={{ marginHorizontal: 22, marginTop: 20, gap: 10 }}>
          <Pressable onPress={() => act('approve')} style={({ pressed }) => [s.btn, { backgroundColor: C.green, opacity: pressed ? 0.85 : 1 }]}>
            <Text style={[s.btnText, { color: '#04140C' }]}>Approve with fingerprint</Text>
          </Pressable>
          <Pressable onPress={() => act('reject')} style={({ pressed }) => [s.btn, { backgroundColor: 'transparent', borderWidth: 1, borderColor: C.red, opacity: pressed ? 0.7 : 1 }]}>
            <Text style={[s.btnText, { color: C.red }]}>Reject</Text>
          </Pressable>
        </View>
      ) : !item.done ? (
        <View style={[s.card, { marginHorizontal: 22, marginTop: 20, borderColor: 'rgba(255,181,71,0.4)' }]}>
          <Text style={{ color: C.amber, fontSize: 16, fontWeight: '800' }}>Approve on the HumanKey device</Text>
          <Text style={{ color: C.dim, fontSize: 14, marginTop: 6, lineHeight: 20 }}>
            This payment is over $10,000 or a check found risk. The device keeps its own record of every account it
            approved, so it can catch a changed bank account even if this laptop is hacked. A phone can't.
          </Text>
        </View>
      ) : null}
      {state ? <Text style={{ color: C.text, marginHorizontal: 22, marginTop: 14, fontSize: 14 }}>{state}</Text> : null}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  logo: { width: 30, height: 30, borderRadius: 9, backgroundColor: '#C9D4FF', alignItems: 'center', justifyContent: 'center' },
  h1: { color: C.text, fontSize: 34, fontWeight: '800', marginTop: 18, letterSpacing: -0.5 },
  h2: { color: C.text, fontSize: 18, fontWeight: '700', marginHorizontal: 22, marginBottom: 10 },
  card: { backgroundColor: C.card, borderRadius: 22, padding: 18, borderWidth: 1, borderColor: C.line },
  row: { marginHorizontal: 22, marginBottom: 10, flexDirection: 'row' },
  amount: { color: C.text, fontSize: 28, fontWeight: '800', letterSpacing: -0.5, marginVertical: 4, fontVariant: ['tabular-nums'] },
  label: { color: C.dim, fontSize: 12, fontWeight: '700', letterSpacing: 1.2, textTransform: 'uppercase' },
  input: { color: C.text, fontSize: 16, borderWidth: 1, borderColor: C.line, borderRadius: 14, padding: 14, marginTop: 8, backgroundColor: 'rgba(0,0,0,0.25)' },
  btn: { borderRadius: 18, paddingVertical: 17, alignItems: 'center', marginTop: 18 },
  btnText: { color: '#fff', fontSize: 17, fontWeight: '800' },
  badge: { borderRadius: 999, paddingHorizontal: 10, paddingVertical: 4, alignSelf: 'flex-start' },
  check: { flexDirection: 'row', gap: 12, paddingVertical: 12, alignItems: 'flex-start' },
  dot: { width: 10, height: 10, borderRadius: 5, marginTop: 4 },
});
