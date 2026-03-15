import { useState, useEffect } from 'react';
import { updateSettings } from '../hooks/useApi';
import { Save, Bell, Clock, Webhook } from 'lucide-react';
import './Settings.css';

export default function Settings() {
  const [pollInterval, setPollInterval] = useState(30);
  const [discordWebhook, setDiscordWebhook] = useState('');
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(false);

  // Load current settings
  useEffect(() => {
    fetch('/api/status').then(r => r.json()).then(data => {
      // We'll get these from the status endpoint or a dedicated settings endpoint
    }).catch(() => {});
  }, []);

  const handleSave = async () => {
    setLoading(true);
    try {
      await updateSettings({
        poll_interval: pollInterval,
        discord_webhook: discordWebhook,
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="settings">
      <h2>Settings</h2>

      <div className="settings-grid">
        <div className="setting-card">
          <div className="setting-icon">
            <Clock size={20} />
          </div>
          <div className="setting-body">
            <label className="setting-label">Poll Interval (seconds)</label>
            <p className="setting-desc">How often to check each product for stock</p>
            <input
              type="number"
              min={5}
              max={300}
              value={pollInterval}
              onChange={(e) => setPollInterval(Number(e.target.value))}
            />
          </div>
        </div>

        <div className="setting-card">
          <div className="setting-icon">
            <Bell size={20} />
          </div>
          <div className="setting-body">
            <label className="setting-label">Discord Webhook</label>
            <p className="setting-desc">Get notified in Discord when items are in stock</p>
            <input
              type="url"
              placeholder="https://discord.com/api/webhooks/..."
              value={discordWebhook}
              onChange={(e) => setDiscordWebhook(e.target.value)}
            />
          </div>
        </div>
      </div>

      <div className="settings-actions">
        <button className="save-btn" onClick={handleSave} disabled={loading}>
          <Save size={14} />
          {saved ? 'Saved!' : 'Save Settings'}
        </button>
      </div>

      <div className="settings-info">
        <h3>How It Works</h3>
        <ul>
          <li><strong>Monitor:</strong> Checks each product URL at the poll interval for stock availability</li>
          <li><strong>Notify:</strong> When a product goes in-stock, sends a Discord alert and console notification</li>
          <li><strong>Auto-buy:</strong> If enabled per product, attempts checkout via API or browser automation</li>
          <li><strong>Manual buy:</strong> Click "Buy" on any in-stock product to trigger an immediate checkout attempt</li>
        </ul>
      </div>
    </div>
  );
}
