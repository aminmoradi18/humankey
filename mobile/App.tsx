// HumanKey Approve: the phone tier. The phone may approve only what the gate marks as "phone"
// (clean, $4,000 to $10,000, paying the account this vendor was last paid on). Everything else is
// shown read-only under the Key tab and must be approved on the hardware key.
import Ionicons from '@expo/vector-icons/Ionicons';
import { BlurView } from 'expo-blur';
import * as Haptics from 'expo-haptics';
import { LinearGradient } from 'expo-linear-gradient';
import * as LocalAuthentication from 'expo-local-authentication';
import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator, Animated, BackHandler, Platform, Pressable, RefreshControl, ScrollView, StyleSheet, Text, TextInput, View,
} from 'react-native';
import { SafeAreaProvider, useSafeAreaInsets } from 'react-native-safe-area-context';

import { DEFAULT_SERVER, Devices, Item, Queue, decide, devices, getServer, pair, queue, release, setServer } from './src/api';
import { Identity, checkContract, identity, signDecision } from './src/key';
import { announce, message, setupNotifications } from './src/notify';
import { getItem, setItem } from './src/store';

// Telegram's night palette: blue for "where am I" and for actions, colour only for meaning.
const C = {
  bg: '#0E1621', bar: 'rgba(23,33,43,0.92)', card: '#17212B', card2: '#1E2C3A', line: 'rgba(255,255,255,0.07)',
  text: '#F5F7FA', dim: '#7F8B97', blue: '#2AABEE', blue2: '#3390EC', green: '#4FD18B', amber: '#F5B544', red: '#F2546B',
};
const money = (n: number) => '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const short = (n: number) => '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const tint = (hex: string, a: number) => hex + Math.round(a * 255).toString(16).padStart(2, '0');
type Icon = React.ComponentProps<typeof Ionicons>['name'];
type Tab = 'profile' | 'phone' | 'key' | 'settings';

// One status per invoice, the same words the hardware key shows in its yellow band.
type Status = { tone: string; title: string; text: string };
function status(it: Item): Status {
  const ups = it.findings.filter((f) => f.signal === 'risk_up' || f.signal === 'unknown');
  const bank = it.findings.find((f) => f.check === 'bank_account');
  if (it.last_paid_match === false && bank && /verified/.test(bank.summary))
    return { tone: C.amber, title: 'New account · change verified', text: `${bank.summary}. The hardware key will still ask you to confirm the new account.` };
  if (it.last_paid_match === false) return { tone: C.red, title: 'Account differs from last payment', text: 'Zetaris payment history shows this vendor was last paid to a different bank account. Call the vendor on a known number before approving.' };
  if (it.tier === 'phone') return { tone: C.green, title: 'Clean · phone can approve', text: 'Every check passed, it is between $4,000 and $10,000, and it pays the account this vendor was last paid on.' };
  if (!ups.length && it.amount >= 10000) return { tone: C.green, title: 'Clean · over the limit', text: 'Every check passed. It needs your hardware key only because it is over $10,000.' };
  if (!ups.length) return { tone: C.amber, title: 'Needs the hardware key', text: 'Every check passed, but there is no payment history to compare the bank account with.' };
  return { tone: C.red, title: 'Risk found', text: ups.map((f) => f.summary).join(' · ') };
}

export default function App() {
  return <SafeAreaProvider><Main /></SafeAreaProvider>;
}

function Main() {
  const insets = useSafeAreaInsets();
  const [me, setMe] = useState<Identity | null>(null);
  const [paired, setPaired] = useState<boolean | null>(null);
  const [q, setQ] = useState<Queue | null>(null);
  const [devs, setDevs] = useState<Devices | null>(null);
  const [tab, setTab] = useState<Tab>('phone');
  const [open, setOpen] = useState<string | null>(null);
  const [err, setErr] = useState('');
  const [boot, setBoot] = useState(true);

  useEffect(() => {
    const t = setTimeout(() => setBoot(false), 1300);
    (async () => {
      setMe(await identity());
      setPaired((await getItem('humankey.paired')) === 'yes');
    })();
    return () => clearTimeout(t);
  }, []);

  // Ids already seen, so only payments that arrive while the app runs raise a notification.
  const seen = useRef<Set<string> | null>(null);
  const [banner, setBanner] = useState<Item | null>(null);
  useEffect(() => { if (paired) setupNotifications(); }, [paired]);

  const refresh = useCallback(async () => {
    try {
      const [nq, nd] = await Promise.all([queue(), devices()]);
      const waiting = nq.items.filter((i) => !i.done);
      const fresh = seen.current ? waiting.filter((i) => !seen.current!.has(i.invoice_id)) : [];
      fresh.forEach(announce);
      if (fresh.length) {
        setBanner(fresh[0]);
        Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
      }
      seen.current = new Set(waiting.map((i) => i.invoice_id));
      setQ(nq);
      setDevs(nd);
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

  useEffect(() => {
    const sub = BackHandler.addEventListener('hardwareBackPress', () => {
      if (open) { setOpen(null); return true; }
      if (tab !== 'phone') { setTab('phone'); return true; }
      return false;
    });
    return () => sub.remove();
  }, [open, tab]);

  const items = q?.items || [];
  const item = items.find((i) => i.invoice_id === open);
  const mine = items.filter((i) => i.tier === 'phone' && !i.done);
  const key = items.filter((i) => i.tier === 'hardware' && !i.done);

  if (boot || paired === null || !me) return <Launch />;
  return (
    <View style={s.fill}>
      <StatusBar style="light" />
      {!paired ? (
        <View style={{ flex: 1, paddingTop: insets.top }}><Pairing me={me} onDone={() => setPaired(true)} /></View>
      ) : item ? (
        <View style={{ flex: 1, paddingTop: insets.top, paddingBottom: insets.bottom }}>
          <Detail item={item} onBack={() => setOpen(null)} refresh={refresh} />
        </View>
      ) : (
        <>
          <View style={{ flex: 1, paddingTop: insets.top }}>
            {err ? <Text style={s.err}>{err}</Text> : null}
            {tab === 'profile' ? <Profile me={me} devs={devs} refresh={refresh} />
              : tab === 'settings' ? <Settings me={me} refresh={refresh} onForget={() => setPaired(false)} />
              : tab === 'phone' ? <PhoneTab items={items} mine={mine} onOpen={setOpen} refresh={refresh} />
              : <KeyTab items={items} pending={key} devs={devs} onOpen={setOpen} refresh={refresh} />}
          </View>
          {banner ? <Banner it={banner} top={insets.top} onOpen={() => { setOpen(banner.invoice_id); setBanner(null); }} onDone={() => setBanner(null)} /> : null}
          <TabBar tab={tab} onTab={(t) => { Haptics.selectionAsync(); setTab(t); }}
            badges={{ profile: 0, phone: mine.length, key: key.length, settings: 0 }} bottom={insets.bottom} />
        </>
      )}
    </View>
  );
}

// The app's own launch screen: the icon grows in, then the name.
function Launch() {
  const a = useRef(new Animated.Value(0)).current;
  useEffect(() => { Animated.spring(a, { toValue: 1, useNativeDriver: true, friction: 6 }).start(); }, [a]);
  return (
    <View style={[s.fill, { alignItems: 'center', justifyContent: 'center' }]}>
      <StatusBar style="light" />
      <Animated.Image source={require('./assets/icon.png')}
        style={{ width: 112, height: 112, borderRadius: 28, opacity: a, transform: [{ scale: a.interpolate({ inputRange: [0, 1], outputRange: [0.7, 1] }) }] }} />
      <Animated.Text style={{ color: C.text, fontSize: 26, fontWeight: '800', marginTop: 20, opacity: a }}>HumanKey</Animated.Text>
      <Animated.Text style={{ color: C.dim, fontSize: 14, marginTop: 4, opacity: a }}>The agent explains why. You hold the key.</Animated.Text>
      <ActivityIndicator color={C.blue} style={{ position: 'absolute', bottom: 70 }} />
    </View>
  );
}

// In-app notification: slides down from the top, tap to open, gone after a few seconds.
function Banner({ it, top, onOpen, onDone }: { it: Item; top: number; onOpen: () => void; onDone: () => void }) {
  const y = useRef(new Animated.Value(-140)).current;
  const m = message(it);
  const phone = it.tier === 'phone';
  useEffect(() => {
    Animated.spring(y, { toValue: 0, useNativeDriver: true, friction: 8 }).start();
    const t = setTimeout(() => Animated.timing(y, { toValue: -160, duration: 250, useNativeDriver: true }).start(onDone), 5000);
    return () => clearTimeout(t);
  }, [it.invoice_id]);
  return (
    <Animated.View style={[s.banner, { top: top + 8, transform: [{ translateY: y }] }]}>
      <Pressable onPress={onOpen} style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
        <View style={[s.avatar, { width: 40, height: 40, backgroundColor: phone ? C.blue : C.amber }]}>
          <Ionicons name={phone ? 'finger-print' : 'key'} size={20} color="#fff" />
        </View>
        <View style={{ flex: 1 }}>
          <Text style={{ color: C.text, fontSize: 15, fontWeight: '700' }} numberOfLines={1}>{m.title}</Text>
          <Text style={{ color: C.dim, fontSize: 13, marginTop: 1 }} numberOfLines={1}>{m.body}</Text>
        </View>
        <Text style={{ color: C.dim, fontSize: 12 }}>now</Text>
      </Pressable>
    </Animated.View>
  );
}

// ---------------------------------------------------------------- tab bar
const TABS: { id: Tab; label: string; icon: Icon }[] = [
  { id: 'profile', label: 'Profile', icon: 'person-circle-outline' },
  { id: 'phone', label: 'Phone', icon: 'phone-portrait-outline' },
  { id: 'key', label: 'Key', icon: 'key-outline' },
  { id: 'settings', label: 'Settings', icon: 'settings-outline' },
];

// Floating pill like Telegram (and the shop app): one rounded pill behind the active icon and label.
function TabBar({ tab, onTab, badges, bottom }: { tab: Tab; onTab: (t: Tab) => void; badges: Record<Tab, number>; bottom: number }) {
  return (
    <View pointerEvents="box-none" style={[s.tabWrap, { bottom: bottom + 10 }]}>
      {/* Android has no real blur here, so the bar is solid there; iOS gets the frosted glass. */}
      <Bar style={s.tabbar}>
        {TABS.map((t) => {
          const on = t.id === tab;
          return (
            <Pressable key={t.id} onPress={() => onTab(t.id)} style={s.tabItem} accessibilityRole="tab" accessibilityState={{ selected: on }}>
              {/* Mounted, not recoloured: Android dropped the radius when only the colour changed. */}
              {on ? <View style={[StyleSheet.absoluteFill, s.tabPill]} /> : null}
              <View>
                <Ionicons name={(on ? t.icon.replace('-outline', '') : t.icon) as Icon} size={21} color={on ? C.blue : C.text} />
                {badges[t.id] ? <View style={s.badge}><Text style={s.badgeText}>{badges[t.id]}</Text></View> : null}
              </View>
              <Text style={[s.tabLabel, { color: on ? C.blue : C.text }]}>{t.label}</Text>
            </Pressable>
          );
        })}
      </Bar>
    </View>
  );
}

function Bar({ style, children }: { style: object; children: React.ReactNode }) {
  return Platform.OS === 'ios'
    ? <BlurView intensity={50} tint="dark" style={style}>{children}</BlurView>
    : <View style={[style, { backgroundColor: '#1C2733' }]}>{children}</View>;
}

// ---------------------------------------------------------------- shared pieces
function Title({ title, sub, icon, color }: { title: string; sub: string; icon: Icon; color: string }) {
  return (
    <View style={s.titleRow}>
      <View style={[s.titleIcon, { backgroundColor: tint(color, 0.16) }]}><Ionicons name={icon} size={22} color={color} /></View>
      <View style={{ flex: 1 }}>
        <Text style={s.h1}>{title}</Text>
        <Text style={s.sub}>{sub}</Text>
      </View>
    </View>
  );
}

function Pull({ refresh, children }: { refresh: () => Promise<void> | void; children: React.ReactNode }) {
  const [busy, setBusy] = useState(false);
  return (
    <ScrollView contentContainerStyle={{ paddingBottom: 130 }}
      refreshControl={<RefreshControl refreshing={busy} tintColor={C.dim} colors={[C.blue]} progressBackgroundColor={C.card}
        onRefresh={async () => { setBusy(true); await refresh(); setBusy(false); }} />}>
      {children}
    </ScrollView>
  );
}

function Row({ it, onPress }: { it: Item; onPress: () => void }) {
  const st = status(it);
  return (
    <Pressable onPress={onPress} style={({ pressed }) => [s.row, pressed && { backgroundColor: C.card2 }]}>
      <View style={[s.avatar, { backgroundColor: tint(it.done ? C.dim : st.tone, 0.16) }]}>
        <Ionicons name={it.done ? 'checkmark-done' : it.tier === 'phone' ? 'phone-portrait' : st.tone === C.red ? 'warning' : 'key'}
          size={20} color={it.done ? C.dim : st.tone} />
      </View>
      <View style={{ flex: 1 }}>
        <View style={s.rowTop}>
          <Text style={s.rowTitle}>{it.vendor_id} · {it.invoice_id}</Text>
          <Text style={s.rowAmount}>{short(it.amount)}</Text>
        </View>
        <Text style={{ color: it.done ? C.dim : st.tone, fontSize: 14 }} numberOfLines={1}>{it.done ? `Done · ${it.done}` : st.title}</Text>
      </View>
    </Pressable>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={{ marginTop: 22 }}>
      <Text style={s.section}>{title}</Text>
      <View style={s.group}>{children}</View>
    </View>
  );
}

function Empty({ icon, text }: { icon: Icon; text: string }) {
  return (
    <View style={s.empty}>
      <Ionicons name={icon} size={30} color={C.dim} />
      <Text style={{ color: C.dim, textAlign: 'center', marginTop: 8, lineHeight: 20 }}>{text}</Text>
    </View>
  );
}

// ---------------------------------------------------------------- Phone tab
function PhoneTab({ items, mine, onOpen, refresh }: { items: Item[]; mine: Item[]; onOpen: (id: string) => void; refresh: () => Promise<void> }) {
  const done = items.filter((i) => i.tier === 'phone' && i.done);
  const total = mine.reduce((a, i) => a + i.amount, 0);
  return (
    <Pull refresh={refresh}>
      <Title title="Phone" sub="Clean payments from $4,000 to $10,000" icon="phone-portrait" color={C.blue} />
      <LinearGradient colors={['#2AABEE', '#2283D8']} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={s.hero}>
        <Ionicons name="finger-print" size={150} color="rgba(255,255,255,0.13)" style={{ position: 'absolute', right: -18, bottom: -26 }} />
        <Text style={s.heroLabel}>WAITING FOR YOUR FINGERPRINT</Text>
        <Text style={s.heroAmount}>{money(total)}</Text>
        <Text style={s.heroSub}>{mine.length ? `${mine.length} payment${mine.length > 1 ? 's' : ''} you can approve here` : 'Nothing waiting right now'}</Text>
      </LinearGradient>
      <Section title="TO APPROVE">
        {mine.length ? mine.map((it) => <Row key={it.invoice_id} it={it} onPress={() => onOpen(it.invoice_id)} />)
          : <Empty icon="sparkles-outline" text="Clean invoices under $4,000 pay themselves. Bigger or riskier ones go to the Key tab." />}
      </Section>
      {done.length ? (
        <Section title="APPROVED ON THIS PHONE">
          {done.map((it) => <Row key={it.invoice_id} it={it} onPress={() => onOpen(it.invoice_id)} />)}
        </Section>
      ) : null}
    </Pull>
  );
}

// ---------------------------------------------------------------- Key tab
function KeyArt({ online, id, counter }: { online: boolean; id: string; counter: number }) {
  // A drawing of the real board: the two-colour OLED (yellow band, blue body) and its two buttons.
  return (
    <View style={s.keyCard}>
      <View style={s.board}>
        <View style={s.oled}>
          <View style={s.oledBand}><Text style={s.oledBandText}>HUMANKEY  #{counter}</Text></View>
          <Text style={s.oledBody}>{online ? 'READY' : 'EMULATOR'}</Text>
          <Text style={s.oledFoot}>BT1=YES       BT2=NO</Text>
        </View>
        <View style={{ flexDirection: 'row', gap: 26, marginTop: 12 }}>
          <View style={s.button}><Text style={s.buttonText}>1</Text></View>
          <View style={s.button}><Text style={s.buttonText}>2</Text></View>
        </View>
        <View style={[s.led, { backgroundColor: online ? C.blue : C.dim }]} />
      </View>
      <View style={{ flex: 1, marginLeft: 16 }}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
          <View style={[s.dot, { backgroundColor: online ? C.green : C.amber }]} />
          <Text style={{ color: online ? C.green : C.amber, fontWeight: '700', fontSize: 13 }}>{online ? 'Hardware key in use' : 'Emulator in use'}</Text>
        </View>
        <Text style={{ color: C.text, fontSize: 16, fontWeight: '700', marginTop: 6 }}>{id}</Text>
        <Text style={{ color: C.dim, fontSize: 13, marginTop: 4, lineHeight: 18 }}>
          Signs on the board. Remembers every account you approved and counts the invoices checked.
        </Text>
      </View>
    </View>
  );
}

function KeyTab({ items, pending, devs, onOpen, refresh }: { items: Item[]; pending: Item[]; devs: Devices | null; onOpen: (id: string) => void; refresh: () => Promise<void> }) {
  const keyDev = devs?.devices.find((d) => d.kind === 'key') || devs?.devices.find((d) => d.kind === 'emulator');
  const online = devs?.device_kind === 'serial';
  const risky = pending.filter((i) => status(i).tone !== C.green);
  const clean = pending.filter((i) => status(i).tone === C.green);
  const done = items.filter((i) => i.tier === 'hardware' && i.done);
  return (
    <Pull refresh={refresh}>
      <Title title="Key" sub="Over $10,000, or anything that looks wrong" icon="key" color={C.amber} />
      <KeyArt online={online} id={keyDev?.id || 'no key enrolled'} counter={keyDev?.counter || 0} />
      <Text style={s.note}>These can only be approved by pressing the button on the device. The gate refuses a phone signature on them.</Text>
      {risky.length ? (
        <Section title={`LOOK CLOSELY · ${risky.length}`}>
          {risky.map((it) => <Row key={it.invoice_id} it={it} onPress={() => onOpen(it.invoice_id)} />)}
        </Section>
      ) : null}
      {clean.length ? (
        <Section title={`CLEAN, OVER THE LIMIT · ${clean.length}`}>
          {clean.map((it) => <Row key={it.invoice_id} it={it} onPress={() => onOpen(it.invoice_id)} />)}
        </Section>
      ) : null}
      {!pending.length ? <Section title="WAITING"><Empty icon="shield-checkmark-outline" text="Nothing is waiting for the key." /></Section> : null}
      {done.length ? (
        <Section title="DECIDED ON THE KEY">
          {done.map((it) => <Row key={it.invoice_id} it={it} onPress={() => onOpen(it.invoice_id)} />)}
        </Section>
      ) : null}
    </Pull>
  );
}

// ---------------------------------------------------------------- Profile tab
function Lane({ icon, color, title, range, count, last }: { icon: Icon; color: string; title: string; range: string; count: number; last?: boolean }) {
  return (
    <View style={[s.lane, !last && { borderBottomWidth: 1, borderBottomColor: C.line }]}>
      <View style={[s.avatar, { backgroundColor: tint(color, 0.16), width: 38, height: 38 }]}><Ionicons name={icon} size={19} color={color} /></View>
      <View style={{ flex: 1 }}>
        <Text style={{ color: C.text, fontSize: 15, fontWeight: '600' }}>{title}</Text>
        <Text style={{ color: C.dim, fontSize: 13, marginTop: 1 }}>{range}</Text>
      </View>
      <Text style={{ color: C.dim, fontSize: 13 }}>{count} paid</Text>
    </View>
  );
}

function Profile({ me, devs, refresh }: { me: Identity; devs: Devices | null; refresh: () => Promise<void> }) {
  const paid = devs?.paid || {};
  const auto = short(devs?.limits.auto ?? 4000);
  const top = short(devs?.limits.phone ?? 10000);
  const list = devs?.devices || [];
  const roles: Record<string, { icon: Icon; color: string; role: string }> = {
    key: { icon: 'key', color: C.amber, role: `Hardware key · signs ${top}+ and anything risky` },
    phone: { icon: 'phone-portrait', color: C.blue, role: `Phone · signs clean ${auto} to ${top}` },
    emulator: { icon: 'desktop-outline', color: C.dim, role: 'Browser emulator · for judges without hardware' },
  };
  return (
    <Pull refresh={refresh}>
      <View style={s.profileHead}>
        <LinearGradient colors={['#2AABEE', '#2283D8']} style={s.bigAvatar}>
          <Ionicons name="finger-print" size={40} color="#fff" />
        </LinearGradient>
        <Text style={[s.h1, { marginTop: 12 }]}>{me.id}</Text>
        <Text style={s.sub}>Approves clean payments from {auto} to {top}</Text>
      </View>

      <Section title="WHO APPROVES WHAT">
        <Lane icon="flash" color={C.green} title="Pays itself" range={`Clean, under ${auto}`} count={paid.auto || 0} />
        <Lane icon="phone-portrait" color={C.blue} title="This phone" range={`Clean, ${auto} to ${top}, same account as last time`} count={paid.phone || 0} />
        <Lane icon="key" color={C.amber} title="Hardware key" range={`${top} and over, or any risk`} count={paid.key || 0} last />
      </Section>

      <Section title="TRUSTED DEVICES">
        {list.map((d, i) => {
          const r = roles[d.kind];
          const mineDev = d.id === me.id;
          return (
            <View key={d.id} style={[s.lane, i < list.length - 1 && { borderBottomWidth: 1, borderBottomColor: C.line }]}>
              <View style={[s.avatar, { backgroundColor: tint(r.color, 0.16), width: 38, height: 38 }]}><Ionicons name={r.icon} size={19} color={r.color} /></View>
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontSize: 15, fontWeight: '600' }}>{d.id}{mineDev ? '  (this phone)' : ''}</Text>
                <Text style={{ color: C.dim, fontSize: 13, marginTop: 1 }}>{r.role}</Text>
              </View>
              <Text style={{ color: C.dim, fontSize: 13 }}>#{d.counter}</Text>
            </View>
          );
        })}
        {!list.length ? <Empty icon="hardware-chip-outline" text="No devices yet." /> : null}
      </Section>

    </Pull>
  );
}

// ---------------------------------------------------------------- Settings tab
function SettingRow({ icon, color, title, value, last }: { icon: Icon; color: string; title: string; value: string; last?: boolean }) {
  return (
    <View style={[s.lane, !last && { borderBottomWidth: 1, borderBottomColor: C.line }]}>
      <View style={[s.setIcon, { backgroundColor: color }]}><Ionicons name={icon} size={17} color="#fff" /></View>
      <Text style={{ color: C.text, fontSize: 15, flex: 1 }}>{title}</Text>
      <Text style={{ color: C.dim, fontSize: 14, maxWidth: '55%' }} numberOfLines={1}>{value}</Text>
    </View>
  );
}

function Settings({ me, refresh, onForget }: { me: Identity; refresh: () => Promise<void>; onForget: () => void }) {
  const [server, setSrv] = useState('');
  const [lock, setLock] = useState('checking');
  useEffect(() => {
    getServer().then(setSrv);
    if (Platform.OS === 'web') { setLock('web build, none'); return; }
    LocalAuthentication.getEnrolledLevelAsync().then((l) => setLock(
      l === LocalAuthentication.SecurityLevel.BIOMETRIC_STRONG || l === LocalAuthentication.SecurityLevel.BIOMETRIC_WEAK
        ? 'Fingerprint' : l === LocalAuthentication.SecurityLevel.SECRET ? 'Screen lock PIN' : 'Not set up'));
  }, []);
  return (
    <Pull refresh={refresh}>
      <Title title="Settings" sub="This phone and its connection" icon="settings" color={C.blue} />
      <Section title="CONNECTION">
        <SettingRow icon="server" color={C.blue2} title="Server" value={server.replace(/^https?:\/\//, '')} />
        <SettingRow icon="phone-portrait" color="#8E6CEF" title="Phone id" value={me.id} last />
      </Section>
      <Section title="SECURITY">
        <SettingRow icon="finger-print" color={C.green} title="Confirm with" value={lock} />
        <SettingRow icon="lock-closed" color={C.amber} title="Private key" value="Secure storage" />
        <SettingRow icon="shield-checkmark" color="#E5677A" title="Signature" value="Ed25519" last />
      </Section>
      <Section title="PUBLIC KEY">
        <View style={{ padding: 16 }}>
          <Text style={{ color: C.dim, fontSize: 12, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' }}>{me.pub}</Text>
          <Text style={{ color: C.dim, fontSize: 13, marginTop: 10, lineHeight: 19 }}>
            The private half was made on this phone and never leaves it. The server keeps only this public half.
          </Text>
        </View>
      </Section>
      <Pressable onPress={async () => { await setItem('humankey.paired', 'no'); onForget(); }} style={({ pressed }) => [s.group, s.forget, pressed && { opacity: 0.7 }]}>
        <Text style={{ color: C.red, fontSize: 16, fontWeight: '600' }}>Pair with another server</Text>
      </Pressable>
      <Text style={[s.note, { textAlign: 'center' }]}>HumanKey Approve · phone tier</Text>
    </Pull>
  );
}

// ---------------------------------------------------------------- pairing
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
      <View style={s.profileHead}>
        <LinearGradient colors={['#2AABEE', '#2283D8']} style={s.bigAvatar}><Ionicons name="link" size={38} color="#fff" /></LinearGradient>
        <Text style={[s.h1, { marginTop: 12 }]}>Pair this phone</Text>
        <Text style={[s.sub, { textAlign: 'center', paddingHorizontal: 30 }]}>Enter the 6-digit code the HumanKey server prints in its terminal.</Text>
      </View>
      <View style={[s.group, { padding: 16, marginTop: 8 }]}>
        <Text style={s.label}>SERVER</Text>
        <TextInput value={server} onChangeText={setSrv} autoCapitalize="none" style={s.input} placeholderTextColor={C.dim} />
        <Text style={[s.label, { marginTop: 16 }]}>PAIRING CODE</Text>
        <TextInput value={code} onChangeText={setCode} keyboardType="number-pad" maxLength={6}
          style={[s.input, { fontSize: 30, letterSpacing: 12, textAlign: 'center' }]} placeholder="000000" placeholderTextColor="#3A4A5A" />
        {msg ? <Text style={{ color: C.red, marginTop: 12 }}>{msg}</Text> : null}
        <Pressable onPress={go} disabled={code.length !== 6} style={({ pressed }) => [s.btn, { backgroundColor: C.blue, opacity: code.length !== 6 ? 0.4 : pressed ? 0.8 : 1 }]}>
          <Text style={s.btnText}>Pair</Text>
        </Pressable>
      </View>
      <Text style={[s.note, { marginTop: 14 }]}>This phone is {me.id}. Its private key was made here and never leaves it.</Text>
    </ScrollView>
  );
}

// ---------------------------------------------------------------- detail
function Detail({ item, onBack, refresh }: { item: Item; onBack: () => void; refresh: () => Promise<void> }) {
  const [state, setState] = useState('');
  const working = useRef(false);
  const phone = item.tier === 'phone' && !item.done;

  const act = async (d: 'approve' | 'reject') => {
    if (working.current) return;
    working.current = true;
    try {
      if (d === 'approve' && Platform.OS !== 'web') {
        if ((await LocalAuthentication.getEnrolledLevelAsync()) === LocalAuthentication.SecurityLevel.NONE) {
          setState('Set up a fingerprint or screen lock on this phone first. Approving needs it.');
          Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error);
          return;
        }
        const auth = await LocalAuthentication.authenticateAsync({
          promptMessage: `Approve ${money(item.amount)}`, promptSubtitle: `${item.invoice_id} to ${item.vendor_id}`,
        });
        if (!auth.success) { setState(`Not confirmed (${auth.error ?? 'cancelled'})`); return; }
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
      // The phone hashes the contract itself and checks it says this invoice and this amount.
      const c = await checkContract(p, item.invoice_id, item.amount);
      setState('Signing on this phone…');
      await decide(await signDecision({ ...p, c }, d));
      Haptics.notificationAsync(d === 'approve' ? Haptics.NotificationFeedbackType.Success : Haptics.NotificationFeedbackType.Warning);
      setState(d === 'approve' ? 'Approved. The gate checked the signature and paid.' : 'Rejected. Nothing was paid.');
      await sleep(600);
      refresh();
    } catch (e: any) {
      setState(e.message);
      Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error);
    } finally {
      working.current = false;
    }
  };

  const st = status(item);
  const order = { risk_up: 0, unknown: 1, risk_down: 2, neutral: 3 } as Record<string, number>;
  const findings = [...item.findings].sort((x, y) => (order[x.signal] ?? 3) - (order[y.signal] ?? 3));
  return (
    <View style={{ flex: 1 }}>
      <View style={s.topbar}>
        <Pressable onPress={onBack} hitSlop={12} style={({ pressed }) => [s.back, pressed && { opacity: 0.6 }]}>
          <Ionicons name="chevron-back" size={24} color={C.blue} />
          <Text style={{ color: C.blue, fontSize: 16 }}>Back</Text>
        </Pressable>
        <Text style={{ color: C.text, fontSize: 16, fontWeight: '700' }}>{item.invoice_id}</Text>
        <View style={{ width: 64, alignItems: 'flex-end' }}>
          <Ionicons name={item.tier === 'phone' ? 'phone-portrait' : 'key'} size={20} color={item.tier === 'phone' ? C.blue : C.amber} />
        </View>
      </View>
      <ScrollView contentContainerStyle={{ paddingBottom: 30 }}>
        <View style={{ alignItems: 'center', marginTop: 10 }}>
          <Text style={[s.heroAmount, { color: C.text }]}>{money(item.amount)}</Text>
          <Text style={{ color: C.dim, fontSize: 15 }}>{item.vendor_id} · risk {item.risk}</Text>
        </View>
        <View style={[s.group, { marginTop: 18, padding: 16, backgroundColor: tint(st.tone, 0.1), borderColor: tint(st.tone, 0.4), borderWidth: 1 }]}>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
            <View style={[s.dot, { backgroundColor: st.tone }]} />
            <Text style={{ color: st.tone, fontSize: 16, fontWeight: '800' }}>{st.title}</Text>
          </View>
          <Text style={{ color: C.text, fontSize: 15, lineHeight: 22, marginTop: 6 }}>{st.text}</Text>
          {item.explanation ? <Text style={{ color: C.dim, fontSize: 14, lineHeight: 20, marginTop: 8 }}>Agent: {item.explanation}</Text> : null}
        </View>
        <Section title="WHY · CHECKS RUN THROUGH ZETARIS">
          {findings.map((f, i) => (
            <View key={f.check} style={[s.check, i > 0 && { borderTopWidth: 1, borderTopColor: C.line }]}>
              <View style={[s.dot, { marginTop: 6, backgroundColor: f.signal === 'risk_up' ? C.red : f.signal === 'unknown' ? C.amber : f.signal === 'risk_down' ? C.green : C.dim }]} />
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.dim, fontSize: 12, fontWeight: '700' }}>{f.check.replace(/_/g, ' ').toUpperCase()}</Text>
                <Text style={{ color: C.text, fontSize: 14, marginTop: 2, lineHeight: 20 }}>{f.summary}</Text>
              </View>
            </View>
          ))}
        </Section>
        <Section title={phone ? 'WHAT YOU SIGN' : item.tier === 'phone' ? 'WHAT YOU SIGNED' : 'WHAT THE KEY SHOWS'}>
          <View style={{ padding: 16, backgroundColor: '#070B10', borderRadius: 14 }}>
            <Text style={{ color: '#7DD3FC', fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace', fontSize: 14, lineHeight: 21 }}>{item.key_text}</Text>
          </View>
        </Section>
        {!phone && !item.done ? (
          <View style={[s.group, { marginTop: 16, padding: 16, flexDirection: 'row', gap: 12 }]}>
            <Ionicons name="key" size={22} color={C.amber} />
            <Text style={{ color: C.dim, fontSize: 14, lineHeight: 20, flex: 1 }}>
              Approve this on the HumanKey device. It keeps its own record of every account you approved, so it catches a
              changed bank account even if the laptop is hacked. A phone can't, so the gate refuses a phone signature here.
            </Text>
          </View>
        ) : null}
        {!phone && state ? <Text style={s.note}>{state}</Text> : null}
      </ScrollView>
      {phone && state ? <Text style={s.status}>{state}</Text> : null}
      {phone ? (
        <View style={s.actions}>
          <Pressable onPress={() => act('reject')} style={({ pressed }) => [s.btn, s.ghost, { opacity: pressed ? 0.7 : 1 }]}>
            <Text style={[s.btnText, { color: C.red }]}>Reject</Text>
          </Pressable>
          <Pressable onPress={() => act('approve')} style={({ pressed }) => [s.btn, { flex: 2, marginTop: 0, backgroundColor: C.blue, opacity: pressed ? 0.85 : 1, flexDirection: 'row', justifyContent: 'center', gap: 8 }]}>
            <Ionicons name="finger-print" size={20} color="#fff" />
            <Text style={s.btnText}>Approve</Text>
          </Pressable>
        </View>
      ) : null}
    </View>
  );
}

const s = StyleSheet.create({
  fill: { flex: 1, backgroundColor: C.bg },
  err: { color: C.red, marginHorizontal: 18, marginTop: 8 },
  titleRow: { flexDirection: 'row', alignItems: 'center', gap: 12, paddingHorizontal: 18, paddingTop: 18, paddingBottom: 6 },
  titleIcon: { width: 42, height: 42, borderRadius: 14, alignItems: 'center', justifyContent: 'center' },
  h1: { color: C.text, fontSize: 26, fontWeight: '800', letterSpacing: -0.3 },
  sub: { color: C.dim, fontSize: 14, marginTop: 2 },
  hero: { marginHorizontal: 16, marginTop: 14, borderRadius: 22, padding: 20, overflow: 'hidden' },
  heroLabel: { color: 'rgba(255,255,255,0.8)', fontSize: 11, fontWeight: '800', letterSpacing: 1.4 },
  heroAmount: { color: '#fff', fontSize: 40, fontWeight: '800', letterSpacing: -0.8, marginTop: 6, fontVariant: ['tabular-nums'] },
  heroSub: { color: 'rgba(255,255,255,0.85)', fontSize: 14, marginTop: 2 },
  section: { color: C.dim, fontSize: 12, fontWeight: '700', letterSpacing: 1, marginHorizontal: 30, marginBottom: 8 },
  group: { backgroundColor: C.card, borderRadius: 16, marginHorizontal: 16, overflow: 'hidden' },
  row: { flexDirection: 'row', alignItems: 'center', gap: 12, paddingHorizontal: 14, paddingVertical: 12 },
  rowTop: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'baseline' },
  rowTitle: { color: C.text, fontSize: 15, fontWeight: '600' },
  rowAmount: { color: C.text, fontSize: 15, fontWeight: '700', fontVariant: ['tabular-nums'] },
  avatar: { width: 44, height: 44, borderRadius: 22, alignItems: 'center', justifyContent: 'center' },
  empty: { alignItems: 'center', padding: 22 },
  note: { color: C.dim, fontSize: 13, lineHeight: 19, marginHorizontal: 30, marginTop: 12 },
  keyCard: { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, borderRadius: 20, marginHorizontal: 16, marginTop: 14, padding: 16 },
  board: { width: 132, backgroundColor: '#123B2A', borderRadius: 14, padding: 10, alignItems: 'center', borderWidth: 1, borderColor: '#1E5A40' },
  oled: { width: 110, height: 62, backgroundColor: '#05070A', borderRadius: 4, overflow: 'hidden', borderWidth: 1, borderColor: '#2A2F36' },
  oledBand: { height: 15, backgroundColor: '#05070A', justifyContent: 'center', paddingHorizontal: 4 },
  oledBandText: { color: '#F5D547', fontSize: 7.5, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' },
  oledBody: { color: '#4FC3F7', fontSize: 13, fontWeight: '800', textAlign: 'center', marginTop: 6, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' },
  oledFoot: { color: '#4FC3F7', fontSize: 6.5, textAlign: 'center', marginTop: 8, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' },
  button: { width: 22, height: 22, borderRadius: 11, backgroundColor: '#2B2F36', borderWidth: 2, borderColor: '#0B0D10', alignItems: 'center', justifyContent: 'center' },
  buttonText: { color: '#9AA3AD', fontSize: 9, fontWeight: '700' },
  led: { position: 'absolute', top: 6, right: 6, width: 6, height: 6, borderRadius: 3 },
  dot: { width: 9, height: 9, borderRadius: 5 },
  lane: { flexDirection: 'row', alignItems: 'center', gap: 12, paddingHorizontal: 14, paddingVertical: 12 },
  profileHead: { alignItems: 'center', paddingTop: 26, paddingBottom: 4 },
  bigAvatar: { width: 84, height: 84, borderRadius: 42, alignItems: 'center', justifyContent: 'center' },
  forget: { marginTop: 22, padding: 15, alignItems: 'center' },
  label: { color: C.dim, fontSize: 12, fontWeight: '700', letterSpacing: 1.2 },
  input: { color: C.text, fontSize: 16, borderRadius: 12, padding: 14, marginTop: 8, backgroundColor: C.bg },
  btn: { borderRadius: 14, paddingVertical: 15, alignItems: 'center', marginTop: 18 },
  btnText: { color: '#fff', fontSize: 17, fontWeight: '700' },
  banner: { position: 'absolute', left: 12, right: 12, backgroundColor: '#1C2733', borderRadius: 20, padding: 12,
    borderWidth: StyleSheet.hairlineWidth, borderColor: 'rgba(255,255,255,0.14)', elevation: 12,
    shadowColor: '#000', shadowOpacity: 0.5, shadowRadius: 18, shadowOffset: { width: 0, height: 8 } },
  tabWrap: { position: 'absolute', left: 30, right: 30 },
  tabbar: { flexDirection: 'row', padding: 3, borderRadius: 26, overflow: 'hidden', backgroundColor: 'rgba(28,39,51,0.94)',
    borderWidth: StyleSheet.hairlineWidth, borderColor: 'rgba(255,255,255,0.14)', elevation: 10,
    shadowColor: '#000', shadowOpacity: 0.45, shadowRadius: 16, shadowOffset: { width: 0, height: 6 } },
  tabItem: { flex: 1, alignItems: 'center', justifyContent: 'center', paddingVertical: 5 },
  tabPill: { borderRadius: 22, backgroundColor: 'rgba(42,171,238,0.18)' },
  tabLabel: { fontSize: 10.5, fontWeight: '700', marginTop: 1 },
  setIcon: { width: 30, height: 30, borderRadius: 8, alignItems: 'center', justifyContent: 'center' },
  badge: { position: 'absolute', top: -5, right: -11, minWidth: 16, height: 16, borderRadius: 8, backgroundColor: C.red, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 4, borderWidth: 2, borderColor: '#1C2733' },
  badgeText: { color: '#fff', fontSize: 9, fontWeight: '800' },
  topbar: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 10, paddingVertical: 8 },
  back: { flexDirection: 'row', alignItems: 'center', width: 64 },
  check: { flexDirection: 'row', gap: 12, paddingVertical: 12, paddingHorizontal: 14 },
  status: { color: C.text, fontSize: 14, lineHeight: 20, paddingHorizontal: 18, paddingVertical: 10, backgroundColor: tint(C.blue, 0.12) },
  actions: { flexDirection: 'row', gap: 10, paddingHorizontal: 16, paddingTop: 10, paddingBottom: 12, backgroundColor: C.bg, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: C.line },
  ghost: { flex: 1, marginTop: 0, backgroundColor: 'transparent', borderWidth: 1, borderColor: C.red },
});
