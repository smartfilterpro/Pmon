import { useState, useEffect, useCallback, useRef } from 'react';
import type { StatusResponse } from '../types';

const API_BASE = '/api';

export function useStatus(pollInterval = 3000) {
  const [data, setData] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/status`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const json = await resp.json();
      setData(json);
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

export async function addProduct(url: string, name: string, autoCheckout: boolean) {
  const resp = await fetch(`${API_BASE}/products`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, name, auto_checkout: autoCheckout }),
  });
  return resp.json();
}

export async function removeProduct(url: string) {
  const resp = await fetch(`${API_BASE}/products`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  return resp.json();
}

export async function toggleAutoCheckout(url: string) {
  const resp = await fetch(`${API_BASE}/products/toggle_auto`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  return resp.json();
}

export async function checkoutNow(url: string) {
  const resp = await fetch(`${API_BASE}/products/checkout_now`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  return resp.json();
}

export async function controlMonitor(action: 'start' | 'stop') {
  const resp = await fetch(`${API_BASE}/monitor/${action}`, { method: 'POST' });
  return resp.json();
}

export async function updateSettings(settings: { poll_interval?: number; discord_webhook?: string }) {
  const resp = await fetch(`${API_BASE}/settings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  });
  return resp.json();
}
