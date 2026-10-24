// Notifications: the app polls the HumanKey server on the LAN and tells you when a new payment waits
// for you. No push service and no internet. A system notification is used when the native module is
// there (a development or store build); Expo Go lacks it, so the app also shows its own banner.
import { Platform } from 'react-native';

import type { Item } from './api';

let N: any = null;
try {
  // Loaded lazily: requiring it in Expo Go throws, and the app must keep working without it.
  N = Platform.OS === 'web' ? null : require('expo-notifications');
  N?.setNotificationHandler({
    handleNotification: async () => ({ shouldShowBanner: true, shouldShowList: true, shouldPlaySound: true, shouldSetBadge: false }),
  });
} catch {
  N = null;
}

export const systemNotifications = () => N !== null;

export async function setupNotifications() {
  if (!N) return false;
  try {
    if (Platform.OS === 'android') {
      await N.setNotificationChannelAsync('approvals', {
        name: 'Payments waiting for you', importance: N.AndroidImportance.HIGH, vibrationPattern: [0, 200, 120, 200],
      });
    }
    return (await N.requestPermissionsAsync()).status === 'granted';
  } catch {
    return false;
  }
}

const money = (n: number) => '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });

export function message(it: Item) {
  const phone = it.tier === 'phone';
  return {
    title: phone ? `Approve ${money(it.amount)} on your phone` : `${money(it.amount)} needs the hardware key`,
    body: phone ? `${it.invoice_id} · ${it.vendor_id} · every check passed` : `${it.invoice_id} · ${it.vendor_id} · press the button on the HumanKey device`,
  };
}

export function announce(it: Item) {
  if (!N) return;
  N.scheduleNotificationAsync({
    content: { ...message(it), data: { invoice: it.invoice_id } },
    trigger: Platform.OS === 'android' ? { channelId: 'approvals' } : null,
  }).catch(() => {});
}
