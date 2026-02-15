import React, { useEffect, useMemo, useState } from 'react';

const API_BASE_URL = 'http://localhost:8000/api/v1';

const DEFAULT_RESOURCE_TYPES = [
  's3', 'ec2', 'rds', 'lambda', 'vpc', 'iam', 'dynamodb', 'sns', 'sqs', 'ecs', 'eks',
  'route53', 'cloudfront', 'cloudwatch', 'security_group', 'ebs', 'efs', 'secretsmanager', 'ssm',
  'ecr', 'kms', 'acm', 'apigateway', 'stepfunctions', 'elasticache', 'kinesis', 'codepipeline',
  'codebuild', 'redshift', 'emr', 'sagemaker', 'glue', 'athena', 'waf', 'wellarchitected',
  'subnet', 'nat_gateway', 'internet_gateway', 'eip', 'log_group', 'elb', 'all',
];

function normalizePermissions(list = []) {
  if (!Array.isArray(list)) return [];
  return list.map((item) => ({
    service: String(item?.service || '').trim(),
    can_read: Boolean(item?.can_read),
    can_write: Boolean(item?.can_write),
    can_execute: Boolean(item?.can_execute),
  })).filter((item) => item.service);
}

function AdminPage({ token, user, onLogout, onGoChat }) {
  const [users, setUsers] = useState([]);
  const [resourceTypes, setResourceTypes] = useState(DEFAULT_RESOURCE_TYPES);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [selectedUserId, setSelectedUserId] = useState(null);

  const [newUserName, setNewUserName] = useState('');
  const [newUserPassword, setNewUserPassword] = useState('');
  const [newUserRole, setNewUserRole] = useState('user');

  const [resetPasswordValue, setResetPasswordValue] = useState({});
  const [editorPermissions, setEditorPermissions] = useState([]);
  const [newPermService, setNewPermService] = useState('ec2');

  const headers = useMemo(() => ({
    'Content-Type': 'application/json',
    Authorization: `Bearer ${token}`,
  }), [token]);

  const loadUsers = async () => {
    setLoading(true);
    setError('');
    try {
      const response = await fetch(`${API_BASE_URL}/admin/users`, {
        headers,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data?.detail || 'Failed to load users');
      const list = Array.isArray(data?.users) ? data.users : [];
      setUsers(list);
      if (!selectedUserId && list.length > 0) {
        setSelectedUserId(list[0].id);
        setEditorPermissions(normalizePermissions(list[0].permissions));
      }
    } catch (err) {
      setError(err.message || 'Failed to load users');
    } finally {
      setLoading(false);
    }
  };

  const loadResourceTypes = async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/services`, { headers: { Authorization: `Bearer ${token}` } });
      const data = await response.json();
      if (response.ok && Array.isArray(data?.resource_types)) {
        const items = [...new Set([...data.resource_types, 'all'])];
        if (items.length > 0) setResourceTypes(items);
      }
    } catch (_err) {
      // Keep defaults.
    }
  };

  useEffect(() => {
    loadUsers();
    loadResourceTypes();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectedUser = users.find((item) => item.id === selectedUserId) || null;

  useEffect(() => {
    if (selectedUser) {
      setEditorPermissions(normalizePermissions(selectedUser.permissions));
    }
  }, [selectedUserId, selectedUser]);

  const createUser = async (e) => {
    e.preventDefault();
    if (!newUserName || !newUserPassword) return;
    setLoading(true);
    setError('');
    try {
      const payload = {
        username: newUserName,
        password: newUserPassword,
        role: newUserRole,
        permissions: newUserRole === 'admin'
          ? [{ service: 'all', can_read: true, can_write: true, can_execute: true }]
          : [{ service: 'all', can_read: true, can_write: false, can_execute: false }],
      };
      const response = await fetch(`${API_BASE_URL}/admin/users`, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data?.detail || 'Failed to create user');
      setNewUserName('');
      setNewUserPassword('');
      setNewUserRole('user');
      await loadUsers();
    } catch (err) {
      setError(err.message || 'Failed to create user');
    } finally {
      setLoading(false);
    }
  };

  const setUserActive = async (targetUserId, isActive) => {
    setLoading(true);
    setError('');
    try {
      const response = await fetch(`${API_BASE_URL}/admin/users/${targetUserId}/status`, {
        method: 'PUT',
        headers,
        body: JSON.stringify({ is_active: isActive }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data?.detail || 'Failed to update user status');
      await loadUsers();
    } catch (err) {
      setError(err.message || 'Failed to update user status');
    } finally {
      setLoading(false);
    }
  };

  const resetUserPassword = async (targetUserId) => {
    const pwd = resetPasswordValue[targetUserId] || '';
    if (!pwd) return;
    setLoading(true);
    setError('');
    try {
      const response = await fetch(`${API_BASE_URL}/admin/users/${targetUserId}/password`, {
        method: 'PUT',
        headers,
        body: JSON.stringify({ password: pwd }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data?.detail || 'Failed to change password');
      setResetPasswordValue((prev) => ({ ...prev, [targetUserId]: '' }));
    } catch (err) {
      setError(err.message || 'Failed to change password');
    } finally {
      setLoading(false);
    }
  };

  const addPermissionRow = () => {
    if (!newPermService) return;
    if (editorPermissions.some((row) => row.service === newPermService)) return;
    setEditorPermissions((prev) => [...prev, { service: newPermService, can_read: true, can_write: false, can_execute: false }]);
  };

  const updatePermissionRow = (service, key, value) => {
    setEditorPermissions((prev) => prev.map((row) => (row.service === service ? { ...row, [key]: value } : row)));
  };

  const removePermissionRow = (service) => {
    setEditorPermissions((prev) => prev.filter((row) => row.service !== service));
  };

  const savePermissions = async () => {
    if (!selectedUser) return;
    setLoading(true);
    setError('');
    try {
      const response = await fetch(`${API_BASE_URL}/admin/users/${selectedUser.id}/permissions`, {
        method: 'PUT',
        headers,
        body: JSON.stringify({ permissions: editorPermissions }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data?.detail || 'Failed to save permissions');
      await loadUsers();
    } catch (err) {
      setError(err.message || 'Failed to save permissions');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="admin-page">
      <header className="admin-header">
        <div>
          <h1>Admin Control Center</h1>
          <p>Manage users, passwords, and service-level RBAC permissions.</p>
        </div>
        <div className="admin-header-actions">
          <button type="button" onClick={onGoChat}>Open Chat</button>
          <button type="button" onClick={onLogout}>Logout</button>
        </div>
      </header>

      {error && <div className="admin-error">{error}</div>}

      <div className="admin-grid">
        <section className="admin-card">
          <h2>Create User</h2>
          <form onSubmit={createUser} className="admin-form">
            <label>
              Username
              <input value={newUserName} onChange={(e) => setNewUserName(e.target.value)} disabled={loading} />
            </label>
            <label>
              Password
              <input type="text" value={newUserPassword} onChange={(e) => setNewUserPassword(e.target.value)} disabled={loading} />
            </label>
            <label>
              Role
              <select value={newUserRole} onChange={(e) => setNewUserRole(e.target.value)} disabled={loading}>
                <option value="user">user</option>
                <option value="admin">admin</option>
              </select>
            </label>
            <button type="submit" disabled={loading || !newUserName || !newUserPassword}>Create User</button>
          </form>

          <div className="admin-note">
            Logged in as <strong>{user?.username}</strong> ({user?.role}).
            Default admin credentials are <code>admin/admin</code> until changed.
          </div>
        </section>

        <section className="admin-card wide">
          <h2>Users</h2>
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>User</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>Created</th>
                  <th>Password Reset</th>
                </tr>
              </thead>
              <tbody>
                {users.map((item) => (
                  <tr
                    key={item.id}
                    className={item.id === selectedUserId ? 'selected' : ''}
                    onClick={() => setSelectedUserId(item.id)}
                  >
                    <td>{item.username}</td>
                    <td>{item.role}</td>
                    <td>
                      <label className="inline-toggle" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={Boolean(item.is_active)}
                          onChange={(e) => setUserActive(item.id, e.target.checked)}
                          disabled={loading}
                        />
                        <span>{item.is_active ? 'active' : 'disabled'}</span>
                      </label>
                    </td>
                    <td>{item.created_at || '-'}</td>
                    <td>
                      <div className="inline-input" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="text"
                          placeholder="new password"
                          value={resetPasswordValue[item.id] || ''}
                          onChange={(e) => setResetPasswordValue((prev) => ({ ...prev, [item.id]: e.target.value }))}
                          disabled={loading}
                        />
                        <button
                          type="button"
                          onClick={() => resetUserPassword(item.id)}
                          disabled={loading || !(resetPasswordValue[item.id] || '').trim()}
                        >
                          Set
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <section className="admin-card">
        <h2>RBAC Permissions {selectedUser ? `for ${selectedUser.username}` : ''}</h2>
        {!selectedUser && <p>Select a user from table above.</p>}
        {selectedUser && (
          <>
            <div className="perm-toolbar">
              <select value={newPermService} onChange={(e) => setNewPermService(e.target.value)} disabled={loading}>
                {resourceTypes.map((svc) => (
                  <option key={svc} value={svc}>{svc}</option>
                ))}
              </select>
              <button type="button" onClick={addPermissionRow} disabled={loading}>Add Service Permission</button>
            </div>

            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Service</th>
                    <th>Read</th>
                    <th>Write</th>
                    <th>Execute</th>
                    <th>Remove</th>
                  </tr>
                </thead>
                <tbody>
                  {editorPermissions.map((row) => (
                    <tr key={row.service}>
                      <td>{row.service}</td>
                      <td>
                        <input type="checkbox" checked={row.can_read} onChange={(e) => updatePermissionRow(row.service, 'can_read', e.target.checked)} />
                      </td>
                      <td>
                        <input type="checkbox" checked={row.can_write} onChange={(e) => updatePermissionRow(row.service, 'can_write', e.target.checked)} />
                      </td>
                      <td>
                        <input type="checkbox" checked={row.can_execute} onChange={(e) => updatePermissionRow(row.service, 'can_execute', e.target.checked)} />
                      </td>
                      <td>
                        <button type="button" onClick={() => removePermissionRow(row.service)} disabled={loading || row.service === 'all'}>
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="auth-actions">
              <button type="button" onClick={savePermissions} disabled={loading}>Save Permissions</button>
            </div>
          </>
        )}
      </section>
    </div>
  );
}

export default AdminPage;
