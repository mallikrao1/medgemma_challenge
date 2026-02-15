import React, { useEffect, useMemo, useState } from 'react';
import ChatPage from './pages/ChatPage.jsx';
import AdminPage from './pages/AdminPage.jsx';
import LoginPage from './pages/LoginPage.jsx';

const API_BASE_URL = 'http://localhost:8000/api/v1';
const TOKEN_KEY = 'infra_auth_token';
const USER_KEY = 'infra_auth_user';
const PERMISSIONS_KEY = 'infra_auth_permissions';

function readStoredJson(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw);
  } catch (_e) {
    return fallback;
  }
}

function App() {
  const [token, setToken] = useState(localStorage.getItem(TOKEN_KEY) || '');
  const [user, setUser] = useState(readStoredJson(USER_KEY, null));
  const [permissions, setPermissions] = useState(readStoredJson(PERMISSIONS_KEY, []));
  const [checking, setChecking] = useState(true);
  const [path, setPath] = useState(window.location.pathname || '/');

  useEffect(() => {
    const onPop = () => setPath(window.location.pathname || '/');
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const navigate = (nextPath) => {
    if (!nextPath || nextPath === path) return;
    window.history.pushState({}, '', nextPath);
    setPath(nextPath);
  };

  const authHeaders = useMemo(() => {
    if (!token) return {};
    return { Authorization: `Bearer ${token}` };
  }, [token]);

  useEffect(() => {
    let alive = true;
    async function loadMe() {
      if (!token) {
        setUser(null);
        setPermissions([]);
        setChecking(false);
        return;
      }
      setChecking(true);
      try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 12000);
        let response;
        try {
          response = await fetch(`${API_BASE_URL}/auth/me`, {
            headers: {
              ...authHeaders,
            },
            signal: controller.signal,
          });
        } finally {
          clearTimeout(timeout);
        }
        const data = await response.json();
        if (!response.ok) {
          const err = new Error(data?.detail || 'Authentication failed');
          err.status = response.status;
          throw err;
        }
        if (!alive) return;
        const nextUser = data?.user || null;
        const nextPermissions = Array.isArray(data?.permissions) ? data.permissions : [];
        setUser(nextUser);
        setPermissions(nextPermissions);
        localStorage.setItem(USER_KEY, JSON.stringify(nextUser));
        localStorage.setItem(PERMISSIONS_KEY, JSON.stringify(nextPermissions));
      } catch (error) {
        if (!alive) return;
        if (Number(error?.status || 0) === 401) {
          localStorage.removeItem(TOKEN_KEY);
          localStorage.removeItem(USER_KEY);
          localStorage.removeItem(PERMISSIONS_KEY);
          setToken('');
          setUser(null);
          setPermissions([]);
        }
      } finally {
        if (alive) setChecking(false);
      }
    }
    loadMe();
    return () => {
      alive = false;
    };
  }, [token, authHeaders]);

  const handleLoginSuccess = (payload) => {
    const authToken = payload?.token || '';
    const nextUser = payload?.user || null;
    localStorage.setItem(TOKEN_KEY, authToken);
    localStorage.setItem(USER_KEY, JSON.stringify(nextUser));
    localStorage.setItem(PERMISSIONS_KEY, JSON.stringify(Array.isArray(payload?.permissions) ? payload.permissions : []));
    setToken(authToken);
    setUser(nextUser);
    if (payload?.user?.role === 'admin' && path.startsWith('/admin')) return;
    if (path.startsWith('/admin')) navigate('/');
  };

  const handleLogout = async () => {
    try {
      if (token) {
        await fetch(`${API_BASE_URL}/auth/logout`, {
          method: 'POST',
          headers: {
            ...authHeaders,
          },
        });
      }
    } catch (_e) {
      // Ignore logout API failures.
    }
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem(PERMISSIONS_KEY);
    setToken('');
    setUser(null);
    setPermissions([]);
    if (path.startsWith('/admin')) navigate('/');
  };

  if (checking) {
    return (
      <div className="auth-page">
        <div className="auth-card">
          <h2>Loading session...</h2>
        </div>
      </div>
    );
  }

  if (!token || !user) {
    return <LoginPage onLoginSuccess={handleLoginSuccess} />;
  }

  if (path.startsWith('/admin')) {
    if (user.role !== 'admin') {
      return (
        <div className="auth-page">
          <div className="auth-card">
            <h2>Access denied</h2>
            <p>Admin role is required for this page.</p>
            <div className="auth-actions">
              <button type="button" onClick={() => navigate('/')}>Go to Chat</button>
              <button type="button" onClick={handleLogout}>Logout</button>
            </div>
          </div>
        </div>
      );
    }
    return (
      <AdminPage
        token={token}
        user={user}
        permissions={permissions}
        onLogout={handleLogout}
        onGoChat={() => navigate('/')}
      />
    );
  }

  return (
    <ChatPage
      token={token}
      user={user}
      permissions={permissions}
      onLogout={handleLogout}
      onGoAdmin={() => navigate('/admin')}
    />
  );
}

export default App;
