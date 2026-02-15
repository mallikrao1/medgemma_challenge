import React, { useState } from 'react';

const API_BASE_URL = 'http://localhost:8000/api/v1';

function LoginPage({ onLoginSuccess }) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('admin');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (loading) return;
    setError('');
    setLoading(true);
    try {
      const response = await fetch(`${API_BASE_URL}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data?.detail || 'Login failed');
      }
      onLoginSuccess(data);
    } catch (err) {
      setError(err.message || 'Login failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={handleSubmit}>
        <h2>Infra Execution Agent</h2>
        <p>Sign in to use the platform.</p>
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} disabled={loading} />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} disabled={loading} />
        </label>
        {error && <div className="error-line">{error}</div>}
        <button type="submit" disabled={loading || !username || !password}>
          {loading ? 'Signing in...' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}

export default LoginPage;
