// SecureStore on the phone. The web build (screenshots and quick checks only) falls back to
// localStorage, which is NOT secure storage; a real approval phone runs the native app.
import { Platform } from 'react-native';
import * as SecureStore from 'expo-secure-store';

const web = Platform.OS === 'web';

export const getItem = (k: string): Promise<string | null> =>
  web ? Promise.resolve(globalThis.localStorage?.getItem(k) ?? null) : SecureStore.getItemAsync(k);

export const setItem = (k: string, v: string): Promise<void> =>
  web ? Promise.resolve(globalThis.localStorage?.setItem(k, v)) : SecureStore.setItemAsync(k, v);
