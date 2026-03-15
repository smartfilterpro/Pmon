import { CheckCircle, XCircle, Loader, Clock } from 'lucide-react';
import type { CheckoutEntry } from '../types';
import './CheckoutLog.css';

interface Props {
  checkouts: CheckoutEntry[];
}

const STATUS_CONFIG = {
  success: { icon: CheckCircle, color: 'var(--green)', label: 'Success' },
  failed: { icon: XCircle, color: 'var(--red)', label: 'Failed' },
  attempting: { icon: Loader, color: 'var(--accent-yellow)', label: 'Attempting' },
  idle: { icon: Clock, color: 'var(--text-secondary)', label: 'Idle' },
};

export default function CheckoutLog({ checkouts }: Props) {
  if (checkouts.length === 0) {
    return (
      <div className="log-empty">
        <Clock size={48} strokeWidth={1} />
        <p>No checkout attempts yet</p>
        <p className="log-hint">
          Checkout attempts will appear here when products are purchased
        </p>
      </div>
    );
  }

  const sorted = [...checkouts].reverse();

  return (
    <div className="checkout-log">
      <div className="log-header">
        <h2>Recent Checkout Attempts</h2>
        <span className="log-count">{checkouts.length} total</span>
      </div>

      <div className="log-entries">
        {sorted.map((c, i) => {
          const cfg = STATUS_CONFIG[c.status] || STATUS_CONFIG.idle;
          const Icon = cfg.icon;
          return (
            <div key={`${c.url}-${c.timestamp}-${i}`} className="log-entry">
              <Icon size={18} color={cfg.color} className={c.status === 'attempting' ? 'spin' : ''} />
              <div className="log-info">
                <div className="log-product">
                  <strong>{c.name || 'Unknown'}</strong>
                  <span className="log-retailer">{c.retailer}</span>
                </div>
                {c.order_number && (
                  <div className="log-order">Order #{c.order_number}</div>
                )}
                {c.error && <div className="log-error">{c.error}</div>}
              </div>
              <div className="log-meta">
                <span className="log-status" style={{ color: cfg.color }}>{cfg.label}</span>
                <span className="log-time">{new Date(c.timestamp).toLocaleString()}</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
