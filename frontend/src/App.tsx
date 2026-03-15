import { useState } from 'react';
import { useStatus } from './hooks/useApi';
import Header from './components/Header';
import ProductList from './components/ProductList';
import AddProduct from './components/AddProduct';
import CheckoutLog from './components/CheckoutLog';
import Settings from './components/Settings';
import './App.css';

type Tab = 'monitor' | 'log' | 'settings';

function App() {
  const { data, error, refresh } = useStatus();
  const [tab, setTab] = useState<Tab>('monitor');

  return (
    <div className="app">
      <Header isRunning={data?.is_running ?? false} refresh={refresh} />

      <nav className="tabs">
        <button
          className={`tab ${tab === 'monitor' ? 'active' : ''}`}
          onClick={() => setTab('monitor')}
        >
          Monitor
        </button>
        <button
          className={`tab ${tab === 'log' ? 'active' : ''}`}
          onClick={() => setTab('log')}
        >
          Checkout Log
          {(data?.checkouts.length ?? 0) > 0 && (
            <span className="badge">{data!.checkouts.length}</span>
          )}
        </button>
        <button
          className={`tab ${tab === 'settings' ? 'active' : ''}`}
          onClick={() => setTab('settings')}
        >
          Settings
        </button>
      </nav>

      <main className="content">
        {error && (
          <div className="error-banner">
            Connection error: {error}. Is the backend running?
          </div>
        )}

        {tab === 'monitor' && (
          <>
            <ProductList products={data?.products ?? []} refresh={refresh} />
            <AddProduct refresh={refresh} />
          </>
        )}

        {tab === 'log' && (
          <CheckoutLog checkouts={data?.checkouts ?? []} />
        )}

        {tab === 'settings' && <Settings />}
      </main>
    </div>
  );
}

export default App;
