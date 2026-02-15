#!/bin/bash

cat > frontend/src/App.jsx << 'APPEOF'
import React, { useState } from 'react';
import axios from 'axios';

const API_BASE_URL = 'http://localhost:8000/api/v1';

function App() {
  const [request, setRequest] = useState('');
  const [awsAccessKey, setAwsAccessKey] = useState('');
  const [awsSecretKey, setAwsSecretKey] = useState('');
  const [awsRegion, setAwsRegion] = useState('us-west-2');
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    
    try {
      const response = await axios.post(`${API_BASE_URL}/requests`, {
        natural_language_request: request,
        environment: 'dev',
        cloud_provider: 'aws',
        requester_id: 'admin@localhost',
        aws_access_key: awsAccessKey,
        aws_secret_key: awsSecretKey,
        aws_region: awsRegion
      });
      setResult(response.data);
    } catch (error) {
      setResult({ error: error.message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: '1200px', margin: '0 auto', padding: '2rem', fontFamily: 'system-ui', background: '#0a0e1a', minHeight: '100vh' }}>
      <div style={{ background: '#1a1a2e', padding: '2rem', borderRadius: '12px', marginBottom: '2rem' }}>
        <h1 style={{ color: '#00d9ff', margin: 0 }}>AI Infrastructure Agent</h1>
        <p style={{ color: '#9ca3af', marginTop: '0.5rem' }}>Create, Update, Delete AWS Resources with Natural Language</p>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '2rem' }}>
        <div style={{ background: '#1a1a2e', padding: '2rem', borderRadius: '12px' }}>
          <h2 style={{ color: '#00d9ff', fontSize: '1.5rem', marginBottom: '1.5rem' }}>Command</h2>
          
          <form onSubmit={handleSubmit}>
            <div style={{ marginBottom: '1rem' }}>
              <label style={{ display: 'block', color: '#e5e7eb', marginBottom: '0.5rem', fontWeight: 600 }}>
                What do you want to do?
              </label>
              <textarea
                value={request}
                onChange={(e) => setRequest(e.target.value)}
                placeholder="E.g., Create an S3 bucket named my-data-bucket"
                required
                rows={4}
                style={{
                  width: '100%',
                  padding: '0.75rem',
                  background: '#0f0f1e',
                  border: '2px solid #2a3548',
                  borderRadius: '8px',
                  color: '#e5e7eb',
                  fontSize: '1rem',
                  fontFamily: 'monospace',
                  resize: 'vertical'
                }}
              />
            </div>

            <div style={{ marginBottom: '1rem' }}>
              <label style={{ display: 'block', color: '#e5e7eb', marginBottom: '0.5rem', fontWeight: 600 }}>
                AWS Region
              </label>
              <select
                value={awsRegion}
                onChange={(e) => setAwsRegion(e.target.value)}
                style={{
                  width: '100%',
                  padding: '0.75rem',
                  background: '#0f0f1e',
                  border: '2px solid #2a3548',
                  borderRadius: '8px',
                  color: '#e5e7eb',
                  fontSize: '1rem'
                }}
              >
                <option value="us-east-1">US East (N. Virginia)</option>
                <option value="us-west-2">US West (Oregon)</option>
                <option value="eu-west-1">EU (Ireland)</option>
                <option value="ap-south-1">Asia Pacific (Mumbai)</option>
              </select>
            </div>

            <div style={{ background: '#7c3aed22', padding: '1rem', borderRadius: '8px', marginBottom: '1rem' }}>
              <h3 style={{ color: '#e5e7eb', fontSize: '1rem', marginBottom: '1rem' }}>AWS Credentials</h3>
              
              <div style={{ marginBottom: '0.75rem' }}>
                <label style={{ display: 'block', color: '#e5e7eb', marginBottom: '0.5rem', fontSize: '0.9rem' }}>
                  Access Key ID
                </label>
                <input
                  type="text"
                  value={awsAccessKey}
                  onChange={(e) => setAwsAccessKey(e.target.value)}
                  placeholder="AKIA..."
                  required
                  style={{
                    width: '100%',
                    padding: '0.75rem',
                    background: '#0f0f1e',
                    border: '2px solid #2a3548',
                    borderRadius: '8px',
                    color: '#e5e7eb',
                    fontSize: '1rem',
                    fontFamily: 'monospace'
                  }}
                />
              </div>

              <div>
                <label style={{ display: 'block', color: '#e5e7eb', marginBottom: '0.5rem', fontSize: '0.9rem' }}>
                  Secret Access Key
                </label>
                <input
                  type="password"
                  value={awsSecretKey}
                  onChange={(e) => setAwsSecretKey(e.target.value)}
                  placeholder="Secret key..."
                  required
                  style={{
                    width: '100%',
                    padding: '0.75rem',
                    background: '#0f0f1e',
                    border: '2px solid #2a3548',
                    borderRadius: '8px',
                    color: '#e5e7eb',
                    fontSize: '1rem',
                    fontFamily: 'monospace'
                  }}
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={loading}
              style={{
                width: '100%',
                padding: '1rem',
                background: loading ? '#666' : 'linear-gradient(135deg, #00d9ff, #7c3aed)',
                border: 'none',
                borderRadius: '8px',
                color: 'white',
                fontSize: '1.1rem',
                fontWeight: 600,
                cursor: loading ? 'not-allowed' : 'pointer'
              }}
            >
              {loading ? 'Processing...' : 'Execute Command'}
            </button>
          </form>
        </div>

        <div style={{ background: '#1a1a2e', padding: '2rem', borderRadius: '12px' }}>
          <h2 style={{ color: '#00d9ff', fontSize: '1.5rem', marginBottom: '1.5rem' }}>Result</h2>
          
          {!result ? (
            <div style={{ textAlign: 'center', padding: '3rem', color: '#6b7280' }}>
              <p>Results will appear here after execution</p>
            </div>
          ) : (
            <div>
              {result.status === 'completed' ? (
                <div style={{
                  padding: '1rem',
                  background: '#10b98122',
                  border: '2px solid #10b981',
                  borderRadius: '8px',
                  color: '#10b981',
                  marginBottom: '1rem',
                  fontWeight: 600
                }}>
                  Successfully Executed
                </div>
              ) : (
                <div style={{
                  padding: '1rem',
                  background: '#ef444422',
                  border: '2px solid #ef4444',
                  borderRadius: '8px',
                  color: '#ef4444',
                  marginBottom: '1rem',
                  fontWeight: 600
                }}>
                  Execution Failed
                </div>
              )}

              {result.intent && (
                <div style={{
                  background: '#0f0f1e',
                  padding: '1rem',
                  borderRadius: '8px',
                  marginBottom: '1rem'
                }}>
                  <div style={{ color: '#00d9ff', fontWeight: 600, marginBottom: '0.5rem' }}>
                    Agent Understanding:
                  </div>
                  <div style={{ fontSize: '0.9rem', color: '#9ca3af' }}>
                    <div>Action: {result.intent.action}</div>
                    <div>Resource: {result.intent.resource_type}</div>
                    <div>Name: {result.intent.resource_name || 'N/A'}</div>
                    <div>Region: {result.intent.region}</div>
                  </div>
                </div>
              )}

              <div style={{
                background: '#0f0f1e',
                padding: '1rem',
                borderRadius: '8px',
                maxHeight: '400px',
                overflow: 'auto'
              }}>
                <pre style={{
                  color: '#e5e7eb',
                  fontSize: '0.85rem',
                  fontFamily: 'monospace',
                  whiteSpace: 'pre-wrap',
                  margin: 0
                }}>
                  {JSON.stringify(result.execution_result || result, null, 2)}
                </pre>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default App;
APPEOF

echo "✅ App.jsx updated!"
