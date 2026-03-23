import { useState } from 'react';
import { removeProduct, toggleAutoCheckout, checkoutNow, setQuantity } from '../hooks/useApi';
import { ExternalLink, Trash2, ShoppingCart, Zap, ZapOff, Minus, Plus, AlertTriangle, DollarSign } from 'lucide-react';
import type { Product } from '../types';
import './ProductList.css';

interface Props {
  products: Product[];
  refresh: () => void;
}

const RETAILER_LABELS: Record<string, string> = {
  pokemoncenter: 'PKC',
  target: 'Target',
  bestbuy: 'Best Buy',
  walmart: 'Walmart',
};

const STATUS_LABELS: Record<string, string> = {
  in_stock: 'In Stock',
  out_of_stock: 'Out of Stock',
  unknown: 'Unknown',
  error: 'Error',
};

export default function ProductList({ products, refresh }: Props) {
  const [checkoutFailed, setCheckoutFailed] = useState<Record<string, string>>({});
  const [checkoutLoading, setCheckoutLoading] = useState<Record<string, boolean>>({});

  const handleRemove = async (url: string) => {
    await removeProduct(url);
    setCheckoutFailed(prev => { const n = { ...prev }; delete n[url]; return n; });
    refresh();
  };

  const handleToggleAuto = async (url: string) => {
    await toggleAutoCheckout(url);
    refresh();
  };

  const handleCheckout = async (url: string) => {
    setCheckoutLoading(prev => ({ ...prev, [url]: true }));
    setCheckoutFailed(prev => { const n = { ...prev }; delete n[url]; return n; });
    try {
      const result = await checkoutNow(url);
      if (result.error || result.status === 'failed') {
        setCheckoutFailed(prev => ({
          ...prev,
          [url]: result.error || result.error_message || 'Checkout failed',
        }));
      }
    } catch {
      setCheckoutFailed(prev => ({ ...prev, [url]: 'Checkout request failed' }));
    } finally {
      setCheckoutLoading(prev => ({ ...prev, [url]: false }));
      refresh();
    }
  };

  const handleQty = async (url: string, current: number, delta: number) => {
    const newQty = Math.max(1, current + delta);
    await setQuantity(url, newQty);
    refresh();
  };

  if (products.length === 0) {
    return (
      <div className="empty-state">
        <ShoppingCart size={48} strokeWidth={1} />
        <p>No products being monitored</p>
        <p className="empty-hint">Add a product URL below to get started</p>
      </div>
    );
  }

  return (
    <div className="product-list">
      {products.map((p) => (
        <div key={p.url} className={`product-card status-${p.status}`}>
          {p.image_url && (
            <img
              src={p.image_url}
              alt=""
              className="product-thumb"
              loading="lazy"
            />
          )}
          <div className="product-main">
            <div className="product-header">
              <span className={`retailer-tag retailer-${p.retailer}`}>
                {RETAILER_LABELS[p.retailer] ?? p.retailer}
              </span>
              <span className={`stock-badge stock-${p.status}`}>
                {STATUS_LABELS[p.status] ?? p.status}
              </span>
            </div>

            <h3 className="product-name">{p.name || 'Unnamed Product'}</h3>

            <div className="product-meta">
              <span className={`price-tag ${p.price ? '' : 'price-unavailable'}`}>
                <DollarSign size={12} />
                {p.price || 'Price unavailable'}
              </span>
            </div>

            {p.error && <p className="product-error">{p.error}</p>}

            {checkoutFailed[p.url] && (
              <div className="checkout-failed">
                <AlertTriangle size={12} />
                <span>{checkoutFailed[p.url]}</span>
                <a href={p.url} target="_blank" rel="noopener noreferrer" className="open-buy-link">
                  Open &amp; buy manually
                </a>
              </div>
            )}

            <p className="product-time">
              {p.timestamp ? `Last checked: ${new Date(p.timestamp).toLocaleTimeString()}` : 'Not checked yet'}
            </p>
          </div>

          <div className="product-actions">
            <div className="qty-control">
              <button className="qty-btn" onClick={() => handleQty(p.url, p.quantity, -1)}>
                <Minus size={12} />
              </button>
              <span className="qty-value">{p.quantity}</span>
              <button className="qty-btn" onClick={() => handleQty(p.url, p.quantity, 1)}>
                <Plus size={12} />
              </button>
            </div>

            <button
              className={`action-btn auto-btn ${p.auto_checkout ? 'on' : 'off'}`}
              onClick={() => handleToggleAuto(p.url)}
              title={p.auto_checkout ? 'Disable auto-purchase' : 'Enable auto-purchase'}
            >
              {p.auto_checkout ? <Zap size={14} /> : <ZapOff size={14} />}
              {p.auto_checkout ? 'Auto ON' : 'Auto OFF'}
            </button>

            <button
              className="action-btn buy-btn"
              onClick={() => handleCheckout(p.url)}
              disabled={p.status !== 'in_stock' || checkoutLoading[p.url]}
              title="Buy now"
            >
              <ShoppingCart size={14} />
              {checkoutLoading[p.url] ? 'Buying...' : 'Buy'}
            </button>

            <a href={p.url} target="_blank" rel="noopener noreferrer" className="action-btn open-btn" title="Open product page">
              <ExternalLink size={14} />
              Open
            </a>

            <button
              className="action-btn remove-btn"
              onClick={() => handleRemove(p.url)}
              title="Remove product"
            >
              <Trash2 size={14} />
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
