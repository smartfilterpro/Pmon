import { useState, useEffect } from 'react';
import {
  getSettings, updateSettings, getAccounts, setAccount, testAccount,
  setupTotp, confirmTotp, disableTotp,
} from '../hooks/useApi';
import type { User } from '../types';
import { Save, Bell, Clock, Shield, ShieldCheck, Store, Users, CheckCircle, XCircle, Loader } from 'lucide-react';
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

  // 2FA
  const [totpEnabled, setTotpEnabled] = useState(user.totp_enabled);
  const [totpUri, setTotpUri] = useState('');
  const [totpSecret, setTotpSecret] = useState('');
  const [totpCode, setTotpCode] = useState('');
  const [totpError, setTotpError] = useState('');
  const [totpSuccess, setTotpSuccess] = useState('');

  useEffect(() => {
    getSettings().then(data => {
      setPollInterval(data.settings.poll_interval);
      setDiscordWebhook(data.settings.discord_webhook || '');
    });
    getAccounts().then(data => setAccounts(data.accounts || {}));
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
                      {testLoading[r.id] ? <><Loader size={12} className="spin" /> Testing...</> : 'Test Login'}
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
              <label>Password</label>
              <input type="password" value={retailerPassword}
                onChange={e => setRetailerPassword(e.target.value)}
                placeholder="Enter password" />
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
