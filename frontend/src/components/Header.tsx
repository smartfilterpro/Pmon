import { controlMonitor, logout } from '../hooks/useApi';
import { Play, Square, LogOut, User as UserIcon } from 'lucide-react';
import type { User } from '../types';
import './Header.css';

interface Props {
  isRunning: boolean;
  refresh: () => void;
  user: User;
}

export default function Header({ isRunning, refresh, user }: Props) {
  const handleToggle = async () => {
    await controlMonitor(isRunning ? 'stop' : 'start');
    setTimeout(refresh, 500);
  };

  return (
    <header className="header">
      <div className="header-left">
        <h1 className="logo">P<span>mon</span></h1>
        <span className="subtitle">Pokemon Card Monitor</span>
      </div>

      <div className="header-right">
        <span className={`status-dot ${isRunning ? 'running' : 'stopped'}`} />
        <span className="status-text">{isRunning ? 'Monitoring' : 'Stopped'}</span>
        <button className={`toggle-btn ${isRunning ? 'stop' : 'start'}`} onClick={handleToggle}>
          {isRunning ? <Square size={14} /> : <Play size={14} />}
          {isRunning ? 'Stop' : 'Start'}
        </button>

        <div className="user-info">
          <UserIcon size={14} />
          <span>{user.username}</span>
          <button className="logout-btn" onClick={logout} title="Sign out">
            <LogOut size={14} />
          </button>
        </div>
      </div>
    </header>
  );
}
