#!/bin/bash

echo "🎨 Creating frontend files..."

# Create package.json
cat > frontend/package.json << 'PKGEOF'
{
  "name": "ai-infra-platform-frontend",
  "version": "1.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite --host",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "axios": "^1.6.5",
    "@tanstack/react-query": "^5.17.9",
    "lucide-react": "^0.309.0",
    "framer-motion": "^10.18.0"
  },
  "devDependencies": {
    "@types/react": "^18.2.48",
    "@types/react-dom": "^18.2.18",
    "@vitejs/plugin-react": "^4.2.1",
    "vite": "^5.0.11"
  }
}
PKGEOF

# Create vite.config.js
cat > frontend/vite.config.js << 'VITEEOF'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true
  }
})
VITEEOF

# Create index.html
cat > frontend/index.html << 'HTMLEOF'
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>AI Infrastructure Platform</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
HTMLEOF

# Create src directory structure
mkdir -p frontend/src/components
mkdir -p frontend/src/styles

# Create main.jsx
cat > frontend/src/main.jsx << 'MAINEOF'
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './styles/main.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
MAINEOF

# Create App.jsx (simplified version that works)
cat > frontend/src/App.jsx << 'APPEOF'
import React, { useState } from 'react';
import { QueryClient, QueryClientProvider, useMutation, useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { motion, AnimatePresence } from 'framer-motion';
import { 
  Cloud, Zap, Shield, CheckCircle2, XCircle, Clock, Code2,
  Sparkles, Loader2, Brain, Search, FileCode, ShieldCheck,
  GitBranch, Play, Eye, Terminal, Database, Network, Layers, Activity
} from 'lucide-react';

const queryClient = new QueryClient();
const API_BASE_URL = 'http://localhost:8000/api/v1';

const createRequest = async (data) => {
  const response = await axios.post(`${API_BASE_URL}/requests`, data);
  return response.data;
};

const getProviders = async () => {
  const response = await axios.get(`${API_BASE_URL}/providers`);
  return response.data;
};

const getEnvironments = async () => {
  const response = await axios.get(`${API_BASE_URL}/environments`);
  return response.data;
};

const getHealthStatus = async () => {
  const response = await axios.get(`${API_BASE_URL}/health/detailed`);
  return response.data;
};

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <div className="app">
        <Header />
        <MainContent />
      </div>
    </QueryClientProvider>
  );
}

function Header() {
  const { data: healthData } = useQuery({
    queryKey: ['health'],
    queryFn: getHealthStatus,
    refetchInterval: 5000
  });

  return (
    <motion.header className="header" initial={{ y: -100 }} animate={{ y: 0 }}>
      <div className="header-content">
        <div className="logo">
          <Sparkles className="logo-icon" />
          <div className="logo-text-container">
            <span className="logo-text">AI Infrastructure</span>
            <span className="logo-subtitle">Autonomous Agent Platform</span>
          </div>
        </div>
        <div className="header-status">
          <div className="status-indicator">
            <div className={`status-dot ${healthData?.status === 'healthy' ? 'active' : ''}`} />
            <span className="status-text">
              {healthData?.status === 'healthy' ? 'All Systems Operational' : 'Initializing...'}
            </span>
          </div>
        </div>
      </div>
    </motion.header>
  );
}

function MainContent() {
  const [activeTab, setActiveTab] = useState('agent');

  return (
    <main className="main-content">
      <div className="tabs">
        <button className={`tab ${activeTab === 'agent' ? 'active' : ''}`} onClick={() => setActiveTab('agent')}>
          <Brain size={20} />
          AI Agent
        </button>
      </div>
      <AgentWorkspace />
    </main>
  );
}

function AgentWorkspace() {
  const [request, setRequest] = useState('');
  const [environment, setEnvironment] = useState('dev');
  const [provider, setProvider] = useState('aws');
  const [result, setResult] = useState(null);

  const { data: providersData } = useQuery({ queryKey: ['providers'], queryFn: getProviders });
  const { data: environmentsData } = useQuery({ queryKey: ['environments'], queryFn: getEnvironments });

  const mutation = useMutation({
    mutationFn: createRequest,
    onSuccess: (data) => {
      setResult(data);
    }
  });

  const handleSubmit = (e) => {
    e.preventDefault();
    mutation.mutate({
      natural_language_request: request,
      environment,
      cloud_provider: provider,
      requester_id: 'admin@localhost'
    });
  };

  return (
    <div className="agent-workspace">
      <div className="workspace-header">
        <div className="header-content-agent">
          <Brain className="header-icon" />
          <div>
            <h2 className="workspace-title">AI Agent Workspace</h2>
            <p className="workspace-subtitle">Watch the AI agent work autonomously</p>
          </div>
        </div>
      </div>

      <div className="workspace-grid">
        <div className="workspace-panel input-panel">
          <div className="panel-header">
            <Terminal size={20} />
            <h3>Input</h3>
          </div>
          
          <form onSubmit={handleSubmit} className="agent-form">
            <div className="form-group">
              <label className="form-label">Describe Infrastructure</label>
              <textarea
                className="form-textarea"
                rows={6}
                value={request}
                onChange={(e) => setRequest(e.target.value)}
                placeholder="E.g., Create an S3 bucket with encryption..."
                disabled={mutation.isPending}
                required
              />
            </div>

            <div className="form-row">
              <div className="form-group">
                <label className="form-label">Provider</label>
                <select className="form-select" value={provider} onChange={(e) => setProvider(e.target.value)}>
                  {providersData?.providers.map(p => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </div>

              <div className="form-group">
                <label className="form-label">Environment</label>
                <select className="form-select" value={environment} onChange={(e) => setEnvironment(e.target.value)}>
                  {environmentsData?.environments.map(env => (
                    <option key={env.id} value={env.id}>{env.name}</option>
                  ))}
                </select>
              </div>
            </div>

            <button type="submit" className="submit-button" disabled={mutation.isPending}>
              {mutation.isPending ? (
                <>
                  <Loader2 className="button-icon spinning" />
                  Agent Working...
                </>
              ) : (
                <>
                  <Play className="button-icon" />
                  Start Agent
                </>
              )}
            </button>
          </form>
        </div>

        <div className="workspace-panel output-panel">
          <div className="panel-header">
            <FileCode size={20} />
            <h3>Result</h3>
          </div>
          
          <div className="code-output">
            {!result ? (
              <div className="empty-state">
                <Code2 size={48} className="empty-icon" />
                <p>Results will appear here</p>
              </div>
            ) : (
              <div className="code-container">
                <div className="code-metadata">
                  <div className="metadata-item">
                    <span className="metadata-label">Request ID:</span>
                    <span className="metadata-value">{result.request_id}</span>
                  </div>
                  <div className="metadata-item">
                    <span className="metadata-label">Status:</span>
                    <span className="metadata-value success">{result.status}</span>
                  </div>
                </div>
                <pre className="code-block">
                  <code>{JSON.stringify(result, null, 2)}</code>
                </pre>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
APPEOF

echo "✓ App.jsx created"

# Create minimal CSS
cat > frontend/src/styles/main.css << 'CSSEOF'
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Outfit:wght@300;400;500;600;700;800&display=swap');

:root {
  --color-bg: #0a0e1a;
  --color-surface: #131824;
  --color-surface-elevated: #1a2030;
  --color-border: #2a3548;
  --color-primary: #00d9ff;
  --color-secondary: #7c3aed;
  --color-success: #10b981;
  --color-text: #e5e7eb;
  --color-text-secondary: #9ca3af;
  --font-display: 'Outfit', sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: var(--font-display);
  background: var(--color-bg);
  color: var(--color-text);
  line-height: 1.6;
  min-height: 100vh;
}

.app {
  max-width: 1600px;
  margin: 0 auto;
  padding: 2rem;
}

.header {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 20px;
  padding: 1.5rem 2rem;
  margin-bottom: 2rem;
}

.header-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 1.5rem;
}

.logo {
  display: flex;
  align-items: center;
  gap: 1rem;
}

.logo-icon {
  width: 40px;
  height: 40px;
  color: var(--color-primary);
}

.logo-text {
  font-size: 1.75rem;
  font-weight: 800;
  background: linear-gradient(135deg, var(--color-primary), var(--color-secondary));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

.logo-subtitle {
  font-size: 0.75rem;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 2px;
}

.status-indicator {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.75rem 1.25rem;
  background: rgba(0, 217, 255, 0.1);
  border: 1px solid var(--color-primary);
  border-radius: 12px;
}

.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #ef4444;
}

.status-dot.active {
  background: var(--color-success);
}

.tabs {
  display: flex;
  gap: 1rem;
  margin-bottom: 2rem;
}

.tab {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 1rem 1.75rem;
  background: var(--color-surface);
  border: 2px solid var(--color-border);
  border-radius: 14px;
  color: var(--color-text-secondary);
  font-size: 1rem;
  font-weight: 600;
  cursor: pointer;
  font-family: var(--font-display);
}

.tab.active {
  background: linear-gradient(135deg, rgba(0, 217, 255, 0.15), rgba(124, 58, 237, 0.15));
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.agent-workspace {
  display: flex;
  flex-direction: column;
  gap: 2rem;
}

.workspace-header {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 20px;
  padding: 2rem;
}

.header-content-agent {
  display: flex;
  align-items: center;
  gap: 1.5rem;
}

.header-icon {
  width: 48px;
  height: 48px;
  color: var(--color-primary);
}

.workspace-title {
  font-size: 2.5rem;
  font-weight: 800;
  margin-bottom: 0.5rem;
}

.workspace-subtitle {
  color: var(--color-text-secondary);
  font-size: 1.1rem;
}

.workspace-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
  gap: 1.5rem;
}

.workspace-panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 20px;
  padding: 2rem;
  min-height: 500px;
  display: flex;
  flex-direction: column;
}

.panel-header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 1.5rem;
  padding-bottom: 1rem;
  border-bottom: 2px solid var(--color-border);
}

.panel-header h3 {
  flex: 1;
  font-size: 1.25rem;
  font-weight: 700;
}

.agent-form {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
  flex: 1;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.form-label {
  font-weight: 600;
  font-size: 0.95rem;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.form-textarea,
.form-select {
  background: var(--color-surface-elevated);
  border: 2px solid var(--color-border);
  border-radius: 12px;
  padding: 1rem 1.25rem;
  color: var(--color-text);
  font-size: 1rem;
  font-family: var(--font-display);
}

.form-textarea {
  font-family: var(--font-mono);
  resize: vertical;
}

.form-row {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 1.5rem;
}

.submit-button {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  padding: 1.25rem 2rem;
  background: linear-gradient(135deg, var(--color-primary), var(--color-secondary));
  border: none;
  border-radius: 12px;
  color: white;
  font-size: 1.1rem;
  font-weight: 600;
  cursor: pointer;
  font-family: var(--font-display);
  margin-top: auto;
}

.submit-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.button-icon {
  width: 20px;
  height: 20px;
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.code-output {
  flex: 1;
  display: flex;
  flex-direction: column;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  gap: 1rem;
}

.empty-icon {
  color: var(--color-text-secondary);
  opacity: 0.5;
}

.code-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.code-metadata {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.75rem;
  padding: 1rem;
  background: var(--color-surface-elevated);
  border: 1px solid var(--color-border);
  border-radius: 12px;
}

.metadata-item {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.metadata-label {
  font-size: 0.75rem;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  font-weight: 600;
}

.metadata-value {
  font-size: 0.9rem;
  color: var(--color-text);
  font-family: var(--font-mono);
}

.metadata-value.success {
  color: var(--color-success);
}

.code-block {
  flex: 1;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 0.85rem;
  line-height: 1.8;
  color: var(--color-text);
  background: var(--color-bg);
  padding: 1.5rem;
  border-radius: 12px;
  border: 1px solid var(--color-border);
}
CSSEOF

echo "✓ main.css created"
echo ""
echo "✅ All frontend files created!"
