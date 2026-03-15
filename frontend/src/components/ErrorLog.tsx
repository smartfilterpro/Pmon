import { useState, useEffect } from 'react';
import { getErrors } from '../hooks/useApi';
import type { ErrorEntry } from '../types';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import './ErrorLog.css';

export default function ErrorLog() {
  const [errors, setErrors] = useState<ErrorEntry[]>([]);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    try {
      const data = await getErrors();
      setErrors(data);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  if (loading) return <div className="error-log-loading">Loading errors...</div>;

  return (
    <div className="error-log">
      <div className="error-log-header">
        <h2>Error Log</h2>
        <button className="refresh-btn" onClick={load}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      {errors.length === 0 ? (
        <div className="error-log-empty">
          <AlertTriangle size={48} strokeWidth={1} />
          <p>No errors logged</p>
        </div>
      ) : (
        <div className="error-entries">
          {errors.map((e) => (
            <div key={e.id} className={`error-entry level-${e.level.toLowerCase()}`}>
              <div className="error-entry-header">
                <span className={`error-level level-${e.level.toLowerCase()}`}>{e.level}</span>
                <span className="error-source">{e.source}</span>
                <span className="error-time">{new Date(e.created_at).toLocaleString()}</span>
              </div>
              <p className="error-message">{e.message}</p>
              {e.details && (
                <details className="error-details">
                  <summary>Stack trace</summary>
                  <pre>{e.details}</pre>
                </details>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
