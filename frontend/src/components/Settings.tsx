import { useState, useEffect } from 'react';
import {
  getSettings, updateSettings, getAccounts, setAccount, testAccount,
  setupTotp, confirmTotp, disableTotp, checkAuth,
  getSessions, importSession, deleteSession,
} from '../hooks/useApi';
import type { User } from '../types';
import { Save, Clock, Shield, ShieldCheck, Store, Users, CheckCircle, XCircle, Loader, Cookie, Trash2, Upload } from 'lucide-react';
import { QRCodeSVG } from 'qrcode.react';
import AdminPanel from './AdminPanel';
import './Settings.css';

interface Props {
  user: User;
}

const RETAILERS = [
  { id: 'target', name: 'Target' },
  { id: 'walmart', name: 'Walmart' },
  { id: 'bestbuy', name: 'Best Buy' },
  { id: 'pokemoncenter', name: 'Pokemon Center' },
];

export default function Settings({ user }: Props) {
  const [pollInterval, setPollInterval] = useState(30);
  const [discordWebhook, setDiscordWebhook] = useState('');
  const [saved, setSaved] = useState(false);

  // Retailer accounts
  const [accounts, setAccounts] = useState<Record<string, { email: string; has_password: boolean }>>({});
  const [editRetailer, setEditRetailer] = useState('');
  const [retailerEmail, setRetailerEmail] = useState('');
  const [retailerPassword, setRetailerPassword] = useState('');
  const [accountSaved, setAccountSaved] = useState('');

  // Test login
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; message: string } | null>>({});
  const [testLoading, setTestLoading] = useState<Record<string, boolean>>({});

  // Sessions (cookie import)
  const [sessions, setSessions] = useState<Record<string, { has_session: boolean; cookie_count?: number; updated_at?: string }>>({});
  const [importRetailer, setImportRetailer] = useState('');
  const [importCookies, setImportCookies] = useState('');
  const [importLoading, setImportLoading] = useState(false);
  const [importResult, setImportResult] = useState<{ ok: boolean; message: string } | null>(null);

  // 2FA
  const [totpEnabled, setTotpEnabled] = useState(user.totp_enabled);
  const [totpUri, setTotpUri] = useState('');
  const [totpSecret, setTotpSecret] = useState('');
  const [totpCode, setTotpCode] = useState('');
  const [totpError, setTotpError] = useState('');
  const [totpSuccess, setTotpSuccess] = useState('');

  const refreshSessions = () => {
    getSessions().then(data => setSessions(data.sessions || {}));
  };

  useEffect(() => {
    getSettings().then(data => {
      setPollInterval(data.settings.poll_interval);
      setDiscordWebhook(data.settings.discord_webhook || '');
    });
    getAccounts().then(data => setAccounts(data.accounts || {}));
    refreshSessions();
    // Refresh TOTP status from server (user prop may be stale)
    checkAuth().then(u => { if (u) setTotpEnabled(u.totp_enabled); });
  }, []);

  const handleSaveSettings = async () => {
    await updateSettings({ poll_interval: pollInterval, discord_webhook: discordWebhook });
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const handleSaveAccount = async () => {
    if (!editRetailer) return;
    await setAccount(editRetailer, retailerEmail, retailerPassword);
    setAccountSaved(editRetailer);
    setTimeout(() => setAccountSaved(''), 2000);
    setRetailerPassword('');
    const data = await getAccounts();
    setAccounts(data.accounts || {});
  };

  const handleStartTotp = async () => {
    setTotpError('');
    const data = await setupTotp();
    setTotpUri(data.uri);
    setTotpSecret(data.secret);
  };

  const handleConfirmTotp = async () => {
    setTotpError('');
    const data = await confirmTotp(totpCode);
    if (data.ok) {
      setTotpEnabled(true);
      setTotpUri('');
      setTotpSecret('');
      setTotpCode('');
      setTotpSuccess('2FA enabled successfully!');
      setTimeout(() => setTotpSuccess(''), 3000);
    } else {
      setTotpError('Invalid code. Try again.');
    }
  };

  const handleDisableTotp = async () => {
    await disableTotp();
    setTotpEnabled(false);
  };

  const handleTestLogin = async (retailerId: string) => {
    setTestResult(prev => ({ ...prev, [retailerId]: null }));
    setTestLoading(prev => ({ ...prev, [retailerId]: true }));
    try {
      const data = await testAccount(retailerId);
      setTestResult(prev => ({ ...prev, [retailerId]: { ok: data.ok, message: data.message } }));
    } catch {
      setTestResult(prev => ({ ...prev, [retailerId]: { ok: false, message: 'Request failed' } }));
    } finally {
      setTestLoading(prev => ({ ...prev, [retailerId]: false }));
    }
  };

  const startEditAccount = (retailerId: string) => {
    setEditRetailer(retailerId);
    const acc = accounts[retailerId];
    setRetailerEmail(acc?.email || '');
    setRetailerPassword('');
  };

  const handleImportCookies = async () => {
    if (!importRetailer || !importCookies.trim()) return;
    setImportLoading(true);
    setImportResult(null);
    try {
      // Try to parse as JSON first, fall back to raw cookie string
      let cookies: string | object = importCookies.trim();
      try {
        cookies = JSON.parse(importCookies.trim());
      } catch {
        // Not JSON — send as raw cookie string (name=val; name=val)
      }
      const data = await importSession(importRetailer, cookies as string);
      if (data.ok) {
        setImportResult({ ok: true, message: `Imported ${data.cookie_count} cookies` });
        setImportCookies('');
        refreshSessions();
      } else {
        setImportResult({ ok: false, message: data.error || 'Import failed' });
      }
    } catch {
      setImportResult({ ok: false, message: 'Request failed' });
    } finally {
      setImportLoading(false);
    }
  };

  const handleDeleteSession = async (retailerId: string) => {
    await deleteSession(retailerId);
    refreshSessions();
  };

  return (
    <div className="settings">
      <h2>Settings</h2>

      {/* Admin Panel - only visible to admins */}
      {user.is_admin && (
        <div className="settings-section">
          <h3><Users size={16} /> User Management</h3>
          <p className="section-desc">Approve or reject new account registrations. Manage admin roles.</p>
          <AdminPanel />
        </div>
      )}

      {/* Monitor Settings */}
      <div className="settings-section">
        <h3><Clock size={16} /> Monitor Settings</h3>
        <div className="settings-grid">
          <div className="setting-field">
            <label>Poll Interval (seconds)</label>
            <input type="number" min={5} max={300} value={pollInterval}
              onChange={e => setPollInterval(Number(e.target.value))} />
          </div>
          <div className="setting-field">
            <label>Discord Webhook</label>
            <input type="url" placeholder="https://discord.com/api/webhooks/..."
              value={discordWebhook} onChange={e => setDiscordWebhook(e.target.value)} />
          </div>
        </div>
        <button className="save-btn" onClick={handleSaveSettings}>
          <Save size={14} /> {saved ? 'Saved!' : 'Save Settings'}
        </button>
      </div>

      {/* Retailer Accounts */}
      <div className="settings-section">
        <h3><Store size={16} /> Retailer Accounts</h3>
        <p className="section-desc">
          Add your retailer login credentials for auto-checkout. Make sure you have a payment method saved in each account.
        </p>
        <div className="retailer-list">
          {RETAILERS.map(r => {
            const acc = accounts[r.id];
            return (
              <div key={r.id} className="retailer-row">
                <div className="retailer-info">
                  <strong>{r.name}</strong>
                  {acc?.email ? (
                    <span className="retailer-email">{acc.email} {acc.has_password ? '(configured)' : '(no password)'}</span>
                  ) : (
                    <span className="retailer-none">Not configured</span>
                  )}
                  {testResult[r.id] && (
                    <span className={`test-result ${testResult[r.id]!.ok ? 'test-ok' : 'test-fail'}`}>
                      {testResult[r.id]!.ok ? <CheckCircle size={12} /> : <XCircle size={12} />}
                      {testResult[r.id]!.message}
                    </span>
                  )}
                </div>
                <div className="retailer-actions">
                  {acc?.email && acc?.has_password && (
                    <button
                      className="test-btn"
                      onClick={() => handleTestLogin(r.id)}
                      disabled={testLoading[r.id]}
                    >
                      {testLoading[r.id]
                        ? <><Loader size={12} className="spin" /> Testing...</>
                        : r.id === 'walmart' ? 'Test Session' : 'Test Login'}
                    </button>
                  )}
                  <button className="action-btn" onClick={() => startEditAccount(r.id)}>
                    {acc?.email ? 'Edit' : 'Add'}
                  </button>
                </div>
              </div>
            );
          })}
        </div>

        {editRetailer && (
          <div className="retailer-edit">
            <h4>Edit {RETAILERS.find(r => r.id === editRetailer)?.name} Account</h4>
            <div className="setting-field">
              <label>Email</label>
              <input type="email" value={retailerEmail} onChange={e => setRetailerEmail(e.target.value)} />
            </div>
            <div className="setting-field">
              <label>Password {accounts[editRetailer]?.has_password ? '(saved — leave blank to keep current)' : ''}</label>
              <input type="password" value={retailerPassword}
                onChange={e => setRetailerPassword(e.target.value)}
                placeholder={accounts[editRetailer]?.has_password ? 'Leave blank to keep current password' : 'Enter password'} />
            </div>
            <div className="retailer-edit-actions">
              <button className="save-btn" onClick={handleSaveAccount}>
                <Save size={14} /> {accountSaved === editRetailer ? 'Saved!' : 'Save'}
              </button>
              <button className="cancel-btn" onClick={() => setEditRetailer('')}>Cancel</button>
            </div>
          </div>
        )}
      </div>

      {/* Session Cookies */}
      <div className="settings-section">
        <h3><Cookie size={16} /> Session Cookies</h3>
        <p className="section-desc">
          Import session cookies from your browser to enable checkout. This is required for Target and Walmart
          because their bot protection blocks programmatic login. <strong>How to get cookies:</strong> Log into
          the retailer in your browser, open DevTools (F12) &gt; Application &gt; Cookies, copy all cookies as
          JSON or as a <code>name=value; name=value</code> string.
        </p>
        <div className="retailer-list">
          {RETAILERS.map(r => {
            const s = sessions[r.id];
            return (
              <div key={r.id} className="retailer-row">
                <div className="retailer-info">
                  <strong>{r.name}</strong>
                  {s?.has_session ? (
                    <span className="retailer-email">
                      {s.cookie_count} cookies imported
                      {s.updated_at && <> &middot; {new Date(s.updated_at).toLocaleDateString()}</>}
                    </span>
                  ) : (
                    <span className="retailer-none">No session imported</span>
                  )}
                </div>
                <div className="retailer-actions">
                  <button className="action-btn" onClick={() => {
                    setImportRetailer(importRetailer === r.id ? '' : r.id);
                    setImportCookies('');
                    setImportResult(null);
                  }}>
                    <Upload size={12} /> Import
                  </button>
                  {s?.has_session && (
                    <button className="action-btn" style={{ color: 'var(--red)' }}
                      onClick={() => handleDeleteSession(r.id)}>
                      <Trash2 size={12} />
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {importRetailer && (
          <div className="retailer-edit">
            <h4>Import Cookies for {RETAILERS.find(r => r.id === importRetailer)?.name}</h4>
            <div className="setting-field">
              <label>
                Paste cookies (JSON array, JSON object, or raw cookie string)
              </label>
              <textarea
                className="cookie-input"
                rows={4}
                value={importCookies}
                onChange={e => setImportCookies(e.target.value)}
                placeholder={'[{"name":"SessionID","value":"abc123",...}]\nor\nSessionID=abc123; visitorId=xyz789'}
              />
            </div>
            {importResult && (
              <div className={`test-result ${importResult.ok ? 'test-ok' : 'test-fail'}`} style={{ marginBottom: 8 }}>
                {importResult.ok ? <CheckCircle size={12} /> : <XCircle size={12} />}
                {importResult.message}
              </div>
            )}
            <div className="retailer-edit-actions">
              <button className="save-btn" onClick={handleImportCookies} disabled={importLoading || !importCookies.trim()}>
                {importLoading ? <><Loader size={14} className="spin" /> Importing...</> : <><Upload size={14} /> Import Cookies</>}
              </button>
              <button className="cancel-btn" onClick={() => setImportRetailer('')}>Cancel</button>
            </div>
          </div>
        )}
      </div>

      {/* 2FA */}
      <div className="settings-section">
        <h3>{totpEnabled ? <ShieldCheck size={16} /> : <Shield size={16} />} Two-Factor Authentication</h3>
        {totpSuccess && <div className="totp-success">{totpSuccess}</div>}

        {totpEnabled ? (
          <div>
            <p className="section-desc totp-status-on">2FA is enabled. Your account is protected.</p>
            <button className="danger-btn" onClick={handleDisableTotp}>Disable 2FA</button>
          </div>
        ) : !totpUri ? (
          <div>
            <p className="section-desc">
              Protect your account with an authenticator app (Microsoft Authenticator, Google Authenticator, Duo, etc.)
            </p>
            <button className="save-btn" onClick={handleStartTotp}>
              <Shield size={14} /> Set Up 2FA
            </button>
          </div>
        ) : (
          <div className="totp-setup">
            <p className="section-desc">Scan this QR code with your authenticator app, then enter the 6-digit code below.</p>
            <div className="totp-qr">
              <QRCodeSVG value={totpUri} size={200} bgColor="#ffffff" fgColor="#000000" />
            </div>
            <p className="totp-manual">
              Manual entry: <code>{totpSecret}</code>
            </p>
            {totpError && <div className="totp-error">{totpError}</div>}
            <div className="totp-confirm">
              <input type="text" inputMode="numeric" pattern="[0-9]*" maxLength={6}
                placeholder="Enter 6-digit code" value={totpCode}
                onChange={e => setTotpCode(e.target.value)} />
              <button className="save-btn" onClick={handleConfirmTotp}>Verify & Enable</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
