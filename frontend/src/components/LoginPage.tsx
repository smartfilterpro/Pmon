import { useState, useEffect } from 'react';
import { login, register, hasUsers } from '../hooks/useApi';
import type { User } from '../types';
import './LoginPage.css';

interface Props {
  onLogin: (user: User) => void;
}

export default function LoginPage({ onLogin }: Props) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [totpCode, setTotpCode] = useState('');
  const [needsTotp, setNeedsTotp] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [noUsers, setNoUsers] = useState(false);

  useEffect(() => {
    hasUsers().then(has => {
      if (!has) {
        setMode('register');
        setNoUsers(true);
      }
    });
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      if (mode === 'register') {
        const res = await register(username, password);
        if (res.error) { setError(res.error); return; }
        // Auto-login after register
        const loginRes = await login(username, password);
        if (loginRes.error) {
          if (loginRes.pending) {
            setError('Account created! Waiting for admin approval.');
          } else {
            setError(loginRes.error);
          }
          return;
        }
        onLogin({ user_id: loginRes.user_id, username: loginRes.username, is_admin: loginRes.is_admin ?? false, totp_enabled: loginRes.totp_enabled ?? false });
      } else {
        const res = await login(username, password, needsTotp ? totpCode : undefined);
        if (res.needs_totp) {
          setNeedsTotp(true);
          return;
        }
        if (res.pending) {
          setError('Account pending admin approval.');
          return;
        }
        if (res.error) { setError(res.error); return; }
        onLogin({ user_id: res.user_id, username: res.username, is_admin: res.is_admin, totp_enabled: res.totp_enabled });
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-card">
        <h1 className="login-logo">P<span>mon</span></h1>
        <p className="login-subtitle">
          {noUsers ? 'Create your account to get started' : 'Sign in to your account'}
        </p>

        <form onSubmit={handleSubmit}>
          {error && <div className="login-error">{error}</div>}

          <div className="field">
            <label>Username</label>
            <input
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              autoFocus
              required
            />
          </div>

          <div className="field">
            <label>Password</label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
              minLength={8}
            />
          </div>

          {needsTotp && (
            <div className="field">
              <label>2FA Code</label>
              <input
                type="text"
                inputMode="numeric"
                pattern="[0-9]*"
                maxLength={6}
                value={totpCode}
                onChange={e => setTotpCode(e.target.value)}
                autoFocus
                placeholder="Enter 6-digit code"
              />
            </div>
          )}

          <button type="submit" className="login-btn" disabled={loading}>
            {loading ? 'Please wait...' : mode === 'register' ? 'Create Account' : 'Sign In'}
          </button>
        </form>

        {!noUsers && (
          <p className="login-switch">
            {mode === 'login' ? (
              <>Don't have an account? <button onClick={() => setMode('register')}>Register</button></>
            ) : (
              <>Already have an account? <button onClick={() => setMode('login')}>Sign in</button></>
            )}
          </p>
        )}
      </div>
    </div>
  );
}
