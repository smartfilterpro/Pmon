import { useState, useEffect, useCallback } from 'react';
import type { StatusResponse, User, ErrorEntry, ManagedUser } from '../types';

const API = '/api';

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('pmon_token');
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

async function apiFetch(path: string, opts: RequestInit = {}) {
  const resp = await fetch(`${API}${path}`, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
      ...(opts.headers || {}),
    },
  });
  if (resp.status === 401) {
    localStorage.removeItem('pmon_token');
    window.location.reload();
    throw new Error('Unauthorized');
  }
  return resp;
}

// --- Auth ---

export async function checkAuth(): Promise<User | null> {
  const token = localStorage.getItem('pmon_token');
  if (!token) return null;
  try {
    const resp = await apiFetch('/auth/check');
    if (!resp.ok) return null;
    const data = await resp.json();
    return data as User;
  } catch {
    return null;
  }
}

export async function hasUsers(): Promise<boolean> {
  const resp = await fetch(`${API}/auth/has-users`);
  const data = await resp.json();
  return data.has_users;
}

export async function login(username: string, password: string, totpCode?: string) {
  const resp = await fetch(`${API}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, totp_code: totpCode }),
  });
  const data = await resp.json();
  if (!resp.ok) return { error: data.error, needs_totp: data.needs_totp, pending: data.pending };
  localStorage.setItem('pmon_token', data.token);
  return data;
}

export async function register(username: string, password: string) {
  const resp = await fetch(`${API}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const data = await resp.json();
  if (!resp.ok) return { error: data.error };
  return data;
}

export function logout() {
  localStorage.removeItem('pmon_token');
  window.location.reload();
}

// --- Status ---

export function useStatus(pollInterval = 3000) {
  const [data, setData] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const resp = await apiFetch('/status');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      setData(await resp.json());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, pollInterval);
    return () => clearInterval(id);
  }, [fetchStatus, pollInterval]);

  return { data, error, refresh: fetchStatus };
}

// --- Products ---

export async function addProduct(url: string, name: string, quantity: number, autoCheckout: boolean) {
  const resp = await apiFetch('/products', {
    method: 'POST',
    body: JSON.stringify({ url, name, quantity, auto_checkout: autoCheckout }),
  });
  return resp.json();
}

export async function removeProduct(url: string) {
  return (await apiFetch('/products', {
    method: 'DELETE',
    body: JSON.stringify({ url }),
  })).json();
}

export async function toggleAutoCheckout(url: string) {
  return (await apiFetch('/products/toggle_auto', {
    method: 'POST',
    body: JSON.stringify({ url }),
  })).json();
}

export async function setQuantity(url: string, quantity: number) {
  return (await apiFetch('/products/set_quantity', {
    method: 'POST',
    body: JSON.stringify({ url, quantity }),
  })).json();
}

export async function checkoutNow(url: string) {
  return (await apiFetch('/products/checkout_now', {
    method: 'POST',
    body: JSON.stringify({ url }),
  })).json();
}

// --- Monitor ---

export async function controlMonitor(action: 'start' | 'stop') {
  return (await apiFetch(`/monitor/${action}`, { method: 'POST' })).json();
}

// --- Settings ---

export async function getSettings() {
  return (await apiFetch('/settings')).json();
}

export async function updateSettings(settings: { poll_interval?: number; discord_webhook?: string }) {
  return (await apiFetch('/settings', {
    method: 'POST',
    body: JSON.stringify(settings),
  })).json();
}

// --- Accounts ---

export async function getAccounts() {
  return (await apiFetch('/accounts')).json();
}

export async function setAccount(retailer: string, email: string, password: string, card_cvv: string = '', phone_last4: string = '', account_last_name: string = '') {
  return (await apiFetch('/accounts', {
    method: 'POST',
    body: JSON.stringify({ retailer, email, password, card_cvv, phone_last4, account_last_name }),
  })).json();
}

export async function testAccount(retailer: string) {
  return (await apiFetch('/accounts/test', {
    method: 'POST',
    body: JSON.stringify({ retailer }),
  })).json();
}

// --- Sessions (cookie import for checkout) ---

export async function getSessions() {
  return (await apiFetch('/sessions')).json();
}

export async function importSession(retailer: string, cookies: string) {
  return (await apiFetch('/sessions/import', {
    method: 'POST',
    body: JSON.stringify({ retailer, cookies }),
  })).json();
}

export async function deleteSession(retailer: string) {
  return (await apiFetch(`/sessions/${retailer}`, { method: 'DELETE' })).json();
}

// --- 2FA ---

export async function setupTotp() {
  return (await apiFetch('/auth/totp/setup', { method: 'POST' })).json();
}

export async function confirmTotp(code: string) {
  return (await apiFetch('/auth/totp/confirm', {
    method: 'POST',
    body: JSON.stringify({ code }),
  })).json();
}

export async function disableTotp() {
  return (await apiFetch('/auth/totp/disable', { method: 'POST' })).json();
}

// --- API Key ---

export async function generateApiKey() {
  return (await apiFetch('/settings/generate_api_key', { method: 'POST' })).json();
}

// --- OTP ---

export async function submitOtp(otpId: number, code: string) {
  return (await apiFetch('/otp/submit', {
    method: 'POST',
    body: JSON.stringify({ otp_id: otpId, code }),
  })).json();
}

// --- Errors ---

export async function getErrors(): Promise<ErrorEntry[]> {
  const resp = await apiFetch('/errors');
  const data = await resp.json();
  return data.errors;
}

// --- Admin ---

export async function getAdminUsers(): Promise<ManagedUser[]> {
  const resp = await apiFetch('/admin/users');
  const data = await resp.json();
  return data.users;
}

export async function getPendingUsers(): Promise<ManagedUser[]> {
  const resp = await apiFetch('/admin/pending');
  const data = await resp.json();
  return data.pending;
}

export async function approveUser(userId: number) {
  return (await apiFetch('/admin/approve', {
    method: 'POST',
    body: JSON.stringify({ user_id: userId }),
  })).json();
}

export async function rejectUser(userId: number) {
  return (await apiFetch('/admin/reject', {
    method: 'POST',
    body: JSON.stringify({ user_id: userId }),
  })).json();
}

export async function setUserAdmin(userId: number, isAdmin: boolean) {
  return (await apiFetch('/admin/set_admin', {
    method: 'POST',
    body: JSON.stringify({ user_id: userId, is_admin: isAdmin }),
  })).json();
}
