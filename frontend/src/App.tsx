import { useState, useEffect } from 'react';
import { checkAuth, useStatus } from './hooks/useApi';
import type { User } from './types';
import LoginPage from './components/LoginPage';
import Header from './components/Header';
import ProductList from './components/ProductList';
import AddProduct from './components/AddProduct';
import SearchProducts from './components/SearchProducts';
import CheckoutLog from './components/CheckoutLog';
import Settings from './components/Settings';
import ErrorLog from './components/ErrorLog';
import OtpBanner from './components/OtpBanner';
import './App.css';

type Tab = 'monitor' | 'log' | 'errors' | 'settings';

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<Tab>('monitor');

  useEffect(() => {
    checkAuth().then(u => { setUser(u); setLoading(false); });
  }, []);

  if (loading) {
    return <div className="app loading">Loading...</div>;
  }

  if (!user) {
    return <LoginPage onLogin={setUser} />;
  }

  return <AuthenticatedApp user={user} tab={tab} setTab={setTab} />;
}

function AuthenticatedApp({ user, tab, setTab }: { user: User; tab: Tab; setTab: (t: Tab) => void }) {
  const { data, error, refresh } = useStatus();

  return (
    <div className="app">
      <Header isRunning={data?.is_running ?? false} refresh={refresh} user={user} />

      <nav className="tabs">
        <button className={`tab ${tab === 'monitor' ? 'active' : ''}`} onClick={() => setTab('monitor')}>
          Monitor
        </button>
        <button className={`tab ${tab === 'log' ? 'active' : ''}`} onClick={() => setTab('log')}>
          Checkout Log
          {(data?.checkouts.length ?? 0) > 0 && <span className="badge">{data!.checkouts.length}</span>}
        </button>
        <button className={`tab ${tab === 'errors' ? 'active' : ''}`} onClick={() => setTab('errors')}>
          Errors
        </button>
        <button className={`tab ${tab === 'settings' ? 'active' : ''}`} onClick={() => setTab('settings')}>
          Settings
        </button>
      </nav>

      <main className="content">
        {data?.pending_otp && (
          <OtpBanner otp={data.pending_otp} onSubmitted={refresh} />
        )}
        {error && (
          <div className="error-banner">Connection error: {error}. Is the backend running?</div>
        )}
        {tab === 'monitor' && (
          <>
            {(data?.spend_limit ?? 0) > 0 && (
              <div className="spend-tracker">
                <div className="spend-tracker-label">
                  <span>Spend: ${(data?.total_spent ?? 0).toFixed(2)} / ${(data?.spend_limit ?? 0).toFixed(2)}</span>
                  {(data?.total_spent ?? 0) >= (data?.spend_limit ?? 0) && (
                    <span className="spend-limit-hit">Limit reached — auto-checkout paused</span>
                  )}
                </div>
                <div className="spend-bar">
                  <div
                    className={`spend-bar-fill ${(data?.total_spent ?? 0) >= (data?.spend_limit ?? 0) ? 'spend-bar-full' : ''}`}
                    style={{ width: `${Math.min(100, ((data?.total_spent ?? 0) / (data?.spend_limit ?? 1)) * 100)}%` }}
                  />
                </div>
              </div>
            )}
            <ProductList products={data?.products ?? []} refresh={refresh} />
            <SearchProducts refresh={refresh} />
            <AddProduct refresh={refresh} />
          </>
        )}
        {tab === 'log' && <CheckoutLog checkouts={data?.checkouts ?? []} />}
        {tab === 'errors' && <ErrorLog />}
        {tab === 'settings' && <Settings user={user} onOtpRequired={refresh} />}
      </main>
    </div>
  );
}

export default App;
