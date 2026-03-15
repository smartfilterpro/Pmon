import { useState, useEffect } from 'react';
import { getAdminUsers, getPendingUsers, approveUser, rejectUser, setUserAdmin } from '../hooks/useApi';
import type { ManagedUser } from '../types';
import { UserCheck, UserX, ShieldCheck, Shield, RefreshCw } from 'lucide-react';
import './AdminPanel.css';

export default function AdminPanel() {
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [pending, setPending] = useState<ManagedUser[]>([]);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    try {
      const [u, p] = await Promise.all([getAdminUsers(), getPendingUsers()]);
      setUsers(u);
      setPending(p);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const handleApprove = async (id: number) => {
    await approveUser(id);
    load();
  };

  const handleReject = async (id: number) => {
    await rejectUser(id);
    load();
  };

  const handleToggleAdmin = async (id: number, current: boolean) => {
    await setUserAdmin(id, !current);
    load();
  };

  if (loading) return <div className="admin-loading">Loading users...</div>;

  return (
    <div className="admin-panel">
      {pending.length > 0 && (
        <div className="admin-section pending-section">
          <h4>
            Pending Approval
            <span className="pending-count">{pending.length}</span>
          </h4>
          <div className="user-list">
            {pending.map(u => (
              <div key={u.id} className="user-row pending">
                <div className="user-row-info">
                  <strong>{u.username}</strong>
                  <span className="user-date">Registered {new Date(u.created_at).toLocaleDateString()}</span>
                </div>
                <div className="user-row-actions">
                  <button className="approve-btn" onClick={() => handleApprove(u.id)}>
                    <UserCheck size={14} /> Approve
                  </button>
                  <button className="reject-btn" onClick={() => handleReject(u.id)}>
                    <UserX size={14} /> Reject
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="admin-section">
        <div className="admin-section-header">
          <h4>All Users</h4>
          <button className="refresh-btn-sm" onClick={load}><RefreshCw size={14} /></button>
        </div>
        <div className="user-list">
          {users.map(u => (
            <div key={u.id} className="user-row">
              <div className="user-row-info">
                <strong>{u.username}</strong>
                <div className="user-badges">
                  {u.is_admin ? (
                    <span className="badge-admin">Admin</span>
                  ) : null}
                  {u.approved ? (
                    <span className="badge-approved">Active</span>
                  ) : (
                    <span className="badge-pending">Pending</span>
                  )}
                </div>
                <span className="user-date">
                  {u.last_login ? `Last login: ${new Date(u.last_login).toLocaleDateString()}` : 'Never logged in'}
                </span>
              </div>
              <div className="user-row-actions">
                {u.approved ? (
                  <button
                    className={`admin-toggle ${u.is_admin ? 'is-admin' : ''}`}
                    onClick={() => handleToggleAdmin(u.id, !!u.is_admin)}
                    title={u.is_admin ? 'Remove admin' : 'Make admin'}
                  >
                    {u.is_admin ? <ShieldCheck size={14} /> : <Shield size={14} />}
                    {u.is_admin ? 'Admin' : 'User'}
                  </button>
                ) : (
                  <>
                    <button className="approve-btn" onClick={() => handleApprove(u.id)}>
                      <UserCheck size={14} /> Approve
                    </button>
                    <button className="reject-btn" onClick={() => handleReject(u.id)}>
                      <UserX size={14} /> Reject
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
