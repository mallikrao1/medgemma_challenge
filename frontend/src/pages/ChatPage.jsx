import React, { useEffect, useMemo, useRef, useState } from 'react';

const API_BASE_URL = 'http://localhost:8000/api/v1';
const DEFAULT_MLFLOW_UI_URL = import.meta.env.VITE_MLFLOW_UI_URL || 'http://localhost:5001';
const CHAT_STORE_PREFIX = 'infra_chat_store_v1';
const EXECUTION_TIMEOUT_MS = 5 * 60 * 60 * 1000; // 5 hours for long-running provisioning/deploy flows
const DEFAULT_ASSISTANT_TEXT =
  'Infra Execution Agent is ready. Type in your own words, I will rewrite it into an executable prompt and ask for confirmation.';

const REGIONS = [
  { value: 'us-east-1', label: 'US East (N. Virginia)' },
  { value: 'us-east-2', label: 'US East (Ohio)' },
  { value: 'us-west-1', label: 'US West (N. California)' },
  { value: 'us-west-2', label: 'US West (Oregon)' },
  { value: 'eu-west-1', label: 'EU (Ireland)' },
  { value: 'eu-central-1', label: 'EU (Frankfurt)' },
  { value: 'ap-south-1', label: 'Asia Pacific (Mumbai)' },
  { value: 'ap-southeast-1', label: 'Asia Pacific (Singapore)' },
  { value: 'ap-northeast-1', label: 'Asia Pacific (Tokyo)' },
];

const ENVIRONMENTS = [
  { value: 'dev', label: 'Development' },
  { value: 'qa', label: 'QA / Staging' },
  { value: 'prod', label: 'Production' },
];

const SUGGESTIONS = [
  'Build a production 3-tier web app in us-east-1 with VPC, ALB, private app tier, private RDS, and health checks.',
  'Create a private EKS cluster in us-east-1 with managed node groups and no public worker nodes.',
  'Create an EC2 instance named production-web in us-east-1, make it public, install Tomcat, and open port 8080.',
];

const NON_EC2_STATUS_TYPES = new Set([
  'eks',
  'rds',
  'lambda',
  's3',
  'vpc',
  'elb',
  'ecs',
  'emr',
  'glue',
  'apigateway',
  'cloudfront',
  'redshift',
  'elasticache',
  'dynamodb',
  'sagemaker',
  'codebuild',
  'codepipeline',
  'wellarchitected',
]);
const NON_EC2_PENDING_STATES = new Set([
  'creating',
  'modifying',
  'updating',
  'pending',
  'provisioning',
  'starting',
  'inprogress',
  'in_progress',
  'configuring',
]);

function makeId() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function initialMessages() {
  return [
    {
      id: makeId(),
      role: 'assistant',
      kind: 'text',
      text: DEFAULT_ASSISTANT_TEXT,
    },
  ];
}

function parseValue(value, type = 'string') {
  const text = String(value || '').trim();
  if (!text) return '';
  if (type === 'number' && /^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  if (type === 'boolean') {
    if (/^(true|yes|y|1)$/i.test(text)) return true;
    if (/^(false|no|n|0)$/i.test(text)) return false;
  }
  if (/^(true|false)$/i.test(text)) return text.toLowerCase() === 'true';
  if (/^-?\d+$/.test(text)) return Number(text);
  return text;
}

function toStringList(value) {
  if (Array.isArray(value)) return value.map((v) => String(v));
  return String(value || '')
    .split(',')
    .map((v) => v.trim())
    .filter(Boolean);
}

function extractInstanceId(text) {
  const match = String(text || '').match(/\b(i-[a-z0-9]{8,17})\b/i);
  return match ? match[1] : null;
}

function looksLikeStatusQuery(text) {
  const query = String(text || '').toLowerCase();
  if (!query) return false;
  if (/\b(i-[a-z0-9]{8,17})\b/.test(query)) {
    return /\b(status|update|progress|ready|health|deploy|deployment|check|state|running|pending)\b/.test(query);
  }
  const trimmed = query.trim();
  if (['status', 'progress', 'ready', 'ready?'].includes(trimmed)) return true;
  if (/^\s*(what'?s|what is|is it|any|show|check)\b/.test(query)) {
    return /\b(update|status|progress|ready|state|deployment)\b/.test(query);
  }
  return /\b(deployment status|current state|what is the update|is it ready)\b/.test(query);
}

function parseResourceStrategyPreference(text) {
  const query = String(text || '').toLowerCase().trim();
  if (!query) return null;
  const normalized = query.replace(/[^a-z0-9\s-]/g, ' ').replace(/\s+/g, ' ').trim();
  const looksLikeFullRequest =
    /\b(create|build|deploy|provision|setup|set up|configure|continue|run|update|delete|list|describe)\b/.test(normalized) ||
    /\b(eks|ec2|rds|s3|lambda|vpc|kubernetes|cluster|website|app|application|ingress|nodegroup|load balancer)\b/.test(normalized);
  if (looksLikeFullRequest) return null;

  if (/^(please )?(use )?(existing|already created|reuse|re-use)( resource| resources| one)?$/.test(normalized)) return 'existing';
  if (/^(please )?(create )?(new|from scratch|fresh)( resource| resources| one)?$/.test(normalized)) return 'new';
  return null;
}

function isContinueCommand(text) {
  const query = String(text || '').toLowerCase().trim();
  if (!query) return false;
  const normalized = query.replace(/[^a-z0-9\s-]/g, ' ').replace(/\s+/g, ' ').trim();
  return /^(please )?(continue|proceed|resume|go ahead|next|run)$/.test(normalized);
}

function inferResourceTypeHint(text) {
  const query = String(text || '').toLowerCase();
  if (!query) return null;
  if (/\bec2\b|\binstance\b/.test(query)) return 'ec2';
  if (/\beks\b|\bkubernetes\b/.test(query)) return 'eks';
  if (/\brds\b|\bdatabase\b|\bdb instance\b/.test(query)) return 'rds';
  if (/\blambda\b|\bfunction\b/.test(query)) return 'lambda';
  if (/\bs3\b|\bbucket\b|\bstatic site\b|\bwebsite\b/.test(query)) return 's3';
  if (/\bvpc\b|\bsubnet\b/.test(query)) return 'vpc';
  if (/\belb\b|\balb\b|\bload balancer\b/.test(query)) return 'elb';
  if (/\becs\b|\bfargate\b/.test(query)) return 'ecs';
  if (/\bemr\b|\bspark\b/.test(query)) return 'emr';
  if (/\bglue\b|\betl\b/.test(query)) return 'glue';
  if (/\bapi gateway\b|\bapigateway\b|\bhttp api\b|\brest api\b/.test(query)) return 'apigateway';
  if (/\bcloudfront\b|\bcdn\b/.test(query)) return 'cloudfront';
  if (/\bredshift\b/.test(query)) return 'redshift';
  if (/\belasticache\b|\bredis\b|\bmemcached\b/.test(query)) return 'elasticache';
  if (/\bdynamodb\b|\bnosql\b/.test(query)) return 'dynamodb';
  if (/\bsagemaker\b|\bnotebook\b/.test(query)) return 'sagemaker';
  if (/\bcodebuild\b/.test(query)) return 'codebuild';
  if (/\bcodepipeline\b|\bpipeline\b/.test(query)) return 'codepipeline';
  if (/\bwell[- ]?architected\b|\bworkload\b/.test(query)) return 'wellarchitected';
  return null;
}

function extractResourceNameFromResult(resourceType, intent, exec) {
  const type = String(resourceType || '').toLowerCase();
  if (type === 'rds') return exec?.db_instance_id || intent?.resource_name || null;
  if (type === 'eks') return exec?.cluster_name || intent?.resource_name || null;
  if (type === 'lambda') return exec?.function_name || intent?.resource_name || null;
  if (type === 's3') return exec?.bucket_name || intent?.resource_name || null;
  if (type === 'vpc') return exec?.vpc_id || intent?.resource_name || null;
  if (type === 'elb') return exec?.load_balancer_name || exec?.name || intent?.resource_name || null;
  if (type === 'ecs') return exec?.cluster_name || exec?.service_name || intent?.resource_name || null;
  if (type === 'emr') return exec?.cluster_id || exec?.cluster_name || intent?.resource_name || null;
  if (type === 'glue') return exec?.crawler_name || exec?.database_name || intent?.resource_name || null;
  if (type === 'apigateway') return exec?.api_id || exec?.api_name || intent?.resource_name || null;
  if (type === 'cloudfront') return exec?.distribution_id || intent?.resource_name || null;
  if (type === 'redshift') return exec?.cluster_id || intent?.resource_name || null;
  if (type === 'elasticache') return exec?.cluster_id || intent?.resource_name || null;
  if (type === 'dynamodb') return exec?.table_name || intent?.resource_name || null;
  if (type === 'sagemaker') return exec?.notebook_name || exec?.name || intent?.resource_name || null;
  if (type === 'codebuild') return exec?.project_name || intent?.resource_name || null;
  if (type === 'codepipeline') return exec?.pipeline_name || intent?.resource_name || null;
  if (type === 'wellarchitected') return exec?.workload_id || exec?.workload_name || intent?.resource_name || null;
  return intent?.resource_name || null;
}

function inferNeedsPolling(resourceType, exec) {
  const type = String(resourceType || '').toLowerCase();
  if (Number(exec?.next_retry_seconds || 0) > 0) return true;
  if (type === 's3') {
    const readiness = String(exec?.readiness || '').toLowerCase();
    if (readiness.includes('propagating')) return true;
    return false;
  }
  const state = String(exec?.status || exec?.state || exec?.readiness || '').toLowerCase().replace(/\s+/g, '');
  if (state) return NON_EC2_PENDING_STATES.has(state);
  return false;
}

function deriveExecutionSteps(requestText, region, environment) {
  const text = (requestText || '').toLowerCase();
  const service = text.includes('ec2')
    ? 'EC2'
    : text.includes('eks')
      ? 'EKS'
      : text.includes('spark') || text.includes('emr')
        ? 'EMR/Spark'
        : text.includes('s3')
          ? 'S3'
          : text.includes('rds')
            ? 'RDS'
            : 'AWS resources';

  return [
    `Designing plan for ${service}`,
    `Preparing networking and security for ${environment.toUpperCase()}`,
    `Provisioning compute/data resources in ${region}`,
    'Deploying application/workloads',
    'Validating health checks and returning outputs',
  ];
}

function DeploymentTable({ items }) {
  return (
    <div className="deployments-table-wrap">
      <table className="deployments-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Action</th>
            <th>Resource</th>
            <th>Name</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {items.length === 0 && (
            <tr>
              <td colSpan={5}>No deployments yet.</td>
            </tr>
          )}
          {items.map((item) => (
            <tr key={item.id || item.request_id}>
              <td>{item.created_at || '-'}</td>
              <td>{item.action || '-'}</td>
              <td>{item.resource_type || '-'}</td>
              <td>{item.resource_name || '-'}</td>
              <td>
                <span className={`status-chip ${String(item.status || '').toLowerCase()}`}>{item.status || '-'}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ChatPage({ token, user, onLogout, onGoAdmin }) {
  const chatStoreKey = `${CHAT_STORE_PREFIX}_${user?.id || user?.username || 'anonymous'}`;
  const [awsAccessKey, setAwsAccessKey] = useState('');
  const [awsSecretKey, setAwsSecretKey] = useState('');
  const [awsRegion, setAwsRegion] = useState('us-east-1');
  const [environment, setEnvironment] = useState('dev');
  const [composer, setComposer] = useState('');
  const [loading, setLoading] = useState(false);
  const [loadingSeconds, setLoadingSeconds] = useState(0);
  const [messages, setMessages] = useState(initialMessages);
  const [activeRequest, setActiveRequest] = useState('');
  const [pendingQuestions, setPendingQuestions] = useState([]);
  const [inputVariables, setInputVariables] = useState({});
  const [pendingPromptReview, setPendingPromptReview] = useState(null);
  const [pendingContinuation, setPendingContinuation] = useState(null);
  const [monitorTarget, setMonitorTarget] = useState(null);
  const [monitorSnapshot, setMonitorSnapshot] = useState(null);
  const [lastAutoDeployContext, setLastAutoDeployContext] = useState(null);
  const [serviceMonitorTarget, setServiceMonitorTarget] = useState(null);
  const [lastServiceStatusContext, setLastServiceStatusContext] = useState(null);
  const [serviceStatusByType, setServiceStatusByType] = useState({});
  const [requestVariableMemory, setRequestVariableMemory] = useState({});
  const [lastResumeContext, setLastResumeContext] = useState(null);
  const [lastRequestId, setLastRequestId] = useState(null);
  const [activeRemediation, setActiveRemediation] = useState(null);
  const [mlflowInfo, setMlflowInfo] = useState({
    enabled: false,
    available: false,
    ui_url: DEFAULT_MLFLOW_UI_URL,
  });
  const [deployments, setDeployments] = useState([]);
  const [deploymentsLoading, setDeploymentsLoading] = useState(false);
  const [deploymentsLoadedOnce, setDeploymentsLoadedOnce] = useState(false);
  const [sessionReady, setSessionReady] = useState(false);
  const [sessionMode, setSessionMode] = useState(null);
  const [showSessionChooser, setShowSessionChooser] = useState(false);
  const [selectedExistingRequestId, setSelectedExistingRequestId] = useState('');
  const [sessionRestoreBusy, setSessionRestoreBusy] = useState(false);
  const listRef = useRef(null);
  const lastMonitorStateRef = useRef('');
  const lastMonitorHeartbeatAtRef = useRef(0);
  const lastServiceStateRef = useRef('');
  const lastServiceHeartbeatAtRef = useRef(0);

  const authHeaders = useMemo(
    () => ({
      Authorization: `Bearer ${token}`,
    }),
    [token]
  );

  const pendingQuestion = useMemo(
    () => (pendingQuestions.length > 0 ? pendingQuestions[0] : null),
    [pendingQuestions]
  );

  const unfinishedDeployments = useMemo(() => {
    const latestByRequest = new Map();
    deployments.forEach((item) => {
      const reqId = String(item?.request_id || '').trim();
      if (!reqId || latestByRequest.has(reqId)) return;
      latestByRequest.set(reqId, item);
    });
    return Array.from(latestByRequest.values()).filter((item) => {
      const status = String(item?.status || '').toLowerCase();
      return status !== 'completed';
    });
  }, [deployments]);

  useEffect(() => {
    if (selectedExistingRequestId) return;
    if (unfinishedDeployments.length === 0) return;
    setSelectedExistingRequestId(unfinishedDeployments[0].request_id);
  }, [selectedExistingRequestId, unfinishedDeployments]);

  const executionSteps = useMemo(
    () => deriveExecutionSteps(activeRequest, awsRegion, environment),
    [activeRequest, awsRegion, environment]
  );

  const activeStepIndex = useMemo(
    () => Math.min(Math.floor(loadingSeconds / 3), Math.max(executionSteps.length - 1, 0)),
    [loadingSeconds, executionSteps.length]
  );

  const fetchJson = async (url, options = {}) => {
    const controller = new AbortController();
    const timeoutMs = Number(options?.timeoutMs ?? EXECUTION_TIMEOUT_MS);
    const useTimeout = Number.isFinite(timeoutMs) && timeoutMs > 0;
    const timer = useTimeout ? setTimeout(() => controller.abort(), timeoutMs) : null;
    const { timeoutMs: _timeoutMs, ...rest } = options || {};
    let response;
    try {
      try {
        response = await fetch(url, {
          ...rest,
          headers: {
            ...(rest.headers || {}),
            ...authHeaders,
          },
          signal: controller.signal,
        });
      } catch (error) {
        if (error?.name === 'AbortError') {
          if (useTimeout) {
            const seconds = Math.max(1, Math.round(timeoutMs / 1000));
            throw new Error(
              `Request timed out after ${seconds}s. Long-running provisioning may still be in progress. Ask "what is the update?" to continue.`
            );
          }
          throw new Error('Request was cancelled.');
        }
        throw error;
      }
    } finally {
      if (timer) clearTimeout(timer);
    }
    let data = {};
    try {
      data = await response.json();
    } catch (_e) {
      data = {};
    }
    if (!response.ok) {
      throw new Error(data?.detail || `Request failed (${response.status})`);
    }
    return data;
  };

  const readChatStore = () => {
    try {
      const raw = localStorage.getItem(chatStoreKey);
      if (!raw) return { byRequest: {}, draft: null, selectedMode: null, selectedRequestId: null };
      const parsed = JSON.parse(raw);
      return {
        byRequest: typeof parsed?.byRequest === 'object' && parsed?.byRequest ? parsed.byRequest : {},
        draft: parsed?.draft && typeof parsed.draft === 'object' ? parsed.draft : null,
        selectedMode: parsed?.selectedMode || null,
        selectedRequestId: parsed?.selectedRequestId || null,
      };
    } catch (_e) {
      return { byRequest: {}, draft: null, selectedMode: null, selectedRequestId: null };
    }
  };

  const writeChatStore = (nextValue) => {
    try {
      localStorage.setItem(chatStoreKey, JSON.stringify(nextValue));
    } catch (_e) {
      // Ignore storage write failures.
    }
  };

  const buildConversationSnapshot = () => ({
    messages,
    activeRequest,
    pendingQuestions,
    inputVariables,
    pendingPromptReview,
    pendingContinuation,
    monitorTarget,
    monitorSnapshot,
    lastAutoDeployContext,
    serviceMonitorTarget,
    lastServiceStatusContext,
    serviceStatusByType,
    requestVariableMemory,
    lastResumeContext,
    lastRequestId,
    activeRemediation,
    awsRegion,
    environment,
    savedAt: new Date().toISOString(),
  });

  const restoreConversationSnapshot = (snapshot) => {
    const msgs = Array.isArray(snapshot?.messages) && snapshot.messages.length > 0 ? snapshot.messages : initialMessages();
    setMessages(msgs);
    setActiveRequest(snapshot?.activeRequest || '');
    setPendingQuestions(Array.isArray(snapshot?.pendingQuestions) ? snapshot.pendingQuestions : []);
    setInputVariables(snapshot?.inputVariables && typeof snapshot.inputVariables === 'object' ? snapshot.inputVariables : {});
    setPendingPromptReview(snapshot?.pendingPromptReview || null);
    setPendingContinuation(snapshot?.pendingContinuation || null);
    setMonitorTarget(snapshot?.monitorTarget || null);
    setMonitorSnapshot(snapshot?.monitorSnapshot || null);
    setLastAutoDeployContext(snapshot?.lastAutoDeployContext || null);
    setServiceMonitorTarget(snapshot?.serviceMonitorTarget || null);
    setLastServiceStatusContext(snapshot?.lastServiceStatusContext || null);
    setServiceStatusByType(snapshot?.serviceStatusByType && typeof snapshot.serviceStatusByType === 'object' ? snapshot.serviceStatusByType : {});
    setRequestVariableMemory(snapshot?.requestVariableMemory && typeof snapshot.requestVariableMemory === 'object' ? snapshot.requestVariableMemory : {});
    setLastResumeContext(snapshot?.lastResumeContext || null);
    setLastRequestId(snapshot?.lastRequestId || null);
    setActiveRemediation(snapshot?.activeRemediation || null);
    if (snapshot?.awsRegion) setAwsRegion(snapshot.awsRegion);
    if (snapshot?.environment) setEnvironment(snapshot.environment);
  };

  const resetConversation = () => {
    setMessages(initialMessages());
    setActiveRequest('');
    setPendingQuestions([]);
    setInputVariables({});
    setPendingPromptReview(null);
    setPendingContinuation(null);
    setMonitorTarget(null);
    setMonitorSnapshot(null);
    setLastAutoDeployContext(null);
    setServiceMonitorTarget(null);
    setLastServiceStatusContext(null);
    setServiceStatusByType({});
    setRequestVariableMemory({});
    setLastResumeContext(null);
    setLastRequestId(null);
    setActiveRemediation(null);
    setComposer('');
  };

  const loadDeployments = async () => {
    setDeploymentsLoading(true);
    try {
      const data = await fetchJson(`${API_BASE_URL}/deployments?limit=200`);
      setDeployments(Array.isArray(data?.deployments) ? data.deployments : []);
      return Array.isArray(data?.deployments) ? data.deployments : [];
    } catch (_e) {
      setDeployments([]);
      return [];
    } finally {
      setDeploymentsLoading(false);
      setDeploymentsLoadedOnce(true);
    }
  };

  const loadDeploymentHistory = async (requestId) => {
    if (!requestId) return [];
    const data = await fetchJson(`${API_BASE_URL}/deployments/${requestId}?limit=200`);
    return Array.isArray(data?.deployments) ? data.deployments : [];
  };

  const reconstructMessagesFromHistory = (requestId, history) => {
    const sorted = [...(Array.isArray(history) ? history : [])].sort((a, b) =>
      String(a?.created_at || '').localeCompare(String(b?.created_at || ''))
    );
    const reconstructed = [
      {
        id: makeId(),
        role: 'assistant',
        kind: 'text',
        text: `Loaded existing deployment ${requestId}. I will continue from its latest status.`,
      },
    ];

    const firstPrompt = sorted.find((item) => String(item?.request_text || '').trim())?.request_text;
    if (firstPrompt) {
      reconstructed.push({
        id: makeId(),
        role: 'user',
        kind: 'text',
        text: firstPrompt,
      });
    }

    sorted.forEach((item) => {
      const summary = item?.execution_summary && typeof item.execution_summary === 'object' ? item.execution_summary : {};
      reconstructed.push({
        id: makeId(),
        role: 'assistant',
        kind: 'result',
        payload: {
          status: item?.status || 'failed',
          intent: {
            action: item?.action || null,
            resource_type: item?.resource_type || null,
            resource_name: item?.resource_name || null,
            region: item?.region || null,
          },
          execution_result: {
            success: Boolean(summary?.success),
            error: summary?.error || null,
            final_outcome: summary?.final_outcome || null,
            phases: Array.isArray(summary?.phases) ? summary.phases : [],
            continuation: summary?.continuation || null,
          },
        },
      });
    });

    return reconstructed;
  };

  const getLatestHistoryEntry = (history) => {
    const sorted = [...(Array.isArray(history) ? history : [])].sort((a, b) =>
      String(a?.created_at || '').localeCompare(String(b?.created_at || ''))
    );
    return sorted.length > 0 ? sorted[sorted.length - 1] : null;
  };

  const getSummary = (item) =>
    item?.execution_summary && typeof item.execution_summary === 'object' ? item.execution_summary : {};

  const startNewDeploymentSession = () => {
    resetConversation();
    setSessionMode('new');
    setSessionReady(true);
    setShowSessionChooser(false);
    const store = readChatStore();
    writeChatStore({
      ...store,
      selectedMode: 'new',
      selectedRequestId: null,
    });
  };

  const resumeExistingDeploymentSession = async (requestId) => {
    const selectedId = String(requestId || '').trim();
    if (!selectedId) return;
    setSessionRestoreBusy(true);
    try {
      const store = readChatStore();
      const cached = store?.byRequest?.[selectedId];
      if (cached) {
        restoreConversationSnapshot(cached);
      } else {
        const history = await loadDeploymentHistory(selectedId);
        const reconstructed = reconstructMessagesFromHistory(selectedId, history);
        resetConversation();
        setMessages(reconstructed);
        setLastRequestId(selectedId);
        const latest = getLatestHistoryEntry(history);
        const latestSummary = getSummary(latest);
        if (latest?.request_text) setActiveRequest(latest.request_text);
        else if (latestSummary?.request_text) setActiveRequest(latestSummary.request_text);
        if (latest?.environment) setEnvironment(latest.environment);
        if (latest?.region) setAwsRegion(latest.region);
        if (latestSummary?.resume_context && typeof latestSummary.resume_context === 'object') {
          setLastResumeContext(latestSummary.resume_context);
        }
        if (latestSummary?.continuation && typeof latestSummary.continuation === 'object') {
          setPendingContinuation(latestSummary.continuation);
        }
        const historyResourceType = String(latest?.resource_type || latestSummary?.resource || '').toLowerCase();
        const historyResourceName = String(latest?.resource_name || '').trim();
        if (historyResourceType && historyResourceName && NON_EC2_STATUS_TYPES.has(historyResourceType)) {
          const serviceCtx = {
            resourceType: historyResourceType,
            resourceName: historyResourceName,
            region: latest?.region || awsRegion,
            state: null,
            ready: false,
          };
          setLastServiceStatusContext(serviceCtx);
          setServiceStatusByType((prev) => ({ ...prev, [historyResourceType]: serviceCtx }));
        }
      }
      setSelectedExistingRequestId(selectedId);
      setSessionMode('existing');
      setSessionReady(true);
      setShowSessionChooser(false);
      writeChatStore({
        ...store,
        selectedMode: 'existing',
        selectedRequestId: selectedId,
      });
    } catch (error) {
      appendMessage({
        role: 'assistant',
        kind: 'text',
        text: `Could not restore existing deployment ${selectedId}: ${error.message}`,
      });
    } finally {
      setSessionRestoreBusy(false);
    }
  };

  const resolveLatestUnfinishedDeployment = async () => {
    const list = deployments.length > 0 ? deployments : await loadDeployments();
    if (!Array.isArray(list) || list.length === 0) return null;
    const latestByRequest = new Map();
    list.forEach((item) => {
      const reqId = String(item?.request_id || '').trim();
      if (!reqId || latestByRequest.has(reqId)) return;
      latestByRequest.set(reqId, item);
    });
    const unfinished = Array.from(latestByRequest.values()).filter(
      (item) => String(item?.status || '').toLowerCase() !== 'completed'
    );
    if (unfinished.length === 0) return null;
    unfinished.sort((a, b) => String(b?.created_at || '').localeCompare(String(a?.created_at || '')));
    return unfinished[0];
  };

  useEffect(() => {
    loadDeployments();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let cancelled = false;
    const loadMlflowInfo = async () => {
      try {
        const info = await fetchJson(`${API_BASE_URL}/mlflow/info`);
        if (!cancelled && info && typeof info === 'object') {
          setMlflowInfo({
            enabled: Boolean(info.enabled),
            available: Boolean(info.available),
            ui_url: String(info.ui_url || DEFAULT_MLFLOW_UI_URL),
          });
        }
      } catch (_e) {
        if (!cancelled) {
          setMlflowInfo((prev) => ({ ...prev, ui_url: DEFAULT_MLFLOW_UI_URL }));
        }
      }
    };
    loadMlflowInfo();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  useEffect(() => {
    if (!deploymentsLoadedOnce || sessionReady) return;
    const store = readChatStore();
    setSelectedExistingRequestId(store?.selectedRequestId || '');
    setShowSessionChooser(true);
  }, [deploymentsLoadedOnce, sessionReady]);

  useEffect(() => {
    if (!sessionReady) return;
    const snapshot = buildConversationSnapshot();
    const store = readChatStore();
    const next = {
      ...store,
      selectedMode: sessionMode || (lastRequestId ? 'existing' : 'new'),
      selectedRequestId: lastRequestId || selectedExistingRequestId || null,
    };
    if (lastRequestId) {
      next.byRequest = { ...(store.byRequest || {}), [lastRequestId]: snapshot };
    } else {
      next.draft = snapshot;
    }
    writeChatStore(next);
  }, [
    sessionReady,
    sessionMode,
    selectedExistingRequestId,
    lastRequestId,
    messages,
    activeRequest,
    pendingQuestions,
    inputVariables,
    pendingPromptReview,
    pendingContinuation,
    monitorTarget,
    monitorSnapshot,
    lastAutoDeployContext,
    serviceMonitorTarget,
    lastServiceStatusContext,
    serviceStatusByType,
    requestVariableMemory,
    lastResumeContext,
    activeRemediation,
    awsRegion,
    environment,
  ]);

  useEffect(() => {
    if (!loading) {
      setLoadingSeconds(0);
      return undefined;
    }
    const timer = setInterval(() => {
      setLoadingSeconds((s) => s + 1);
    }, 1000);
    return () => clearInterval(timer);
  }, [loading]);

  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [messages, loading, pendingQuestion, pendingPromptReview]);

  useEffect(() => {
    if (!monitorTarget) return undefined;
    if (!awsAccessKey || !awsSecretKey) return undefined;

    let cancelled = false;

    const checkStatus = async () => {
      try {
        const data = await fetchJson(`${API_BASE_URL}/ec2/status`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            instance_id: monitorTarget.instanceId,
            aws_access_key: awsAccessKey,
            aws_secret_key: awsSecretKey,
            aws_region: monitorTarget.region || awsRegion,
          }),
        });
        if (!data?.success || cancelled) {
          return;
        }

        setMonitorSnapshot(data);
        const stateKey = `${data.state}:${data.instance_status}:${data.system_status}`;
        const now = Date.now();
        const heartbeatDue = now - lastMonitorHeartbeatAtRef.current >= 90000;
        if (stateKey !== lastMonitorStateRef.current) {
          lastMonitorStateRef.current = stateKey;
          lastMonitorHeartbeatAtRef.current = now;
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `Live status for ${data.instance_id}: state=${data.state}, instance=${data.instance_status}, system=${data.system_status}.`,
          });
        } else if (!data.ready && heartbeatDue) {
          lastMonitorHeartbeatAtRef.current = now;
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `Still waiting on ${data.instance_id}: state=${data.state}, instance=${data.instance_status}, system=${data.system_status}. I will keep monitoring every 30s.`,
          });
        }

        if (data.ready && !cancelled) {
          lastMonitorHeartbeatAtRef.current = now;
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `EC2 instance ${data.instance_id} is ready.`,
          });
          if (monitorTarget.deploymentPayload) {
            appendMessage({
              role: 'assistant',
              kind: 'text',
              text: 'Starting automatic deployment now via SSM RunCommand.',
            });
            await executeAutoDeploy(monitorTarget.deploymentPayload);
          } else if (monitorTarget.nextPrompt) {
            setComposer(monitorTarget.nextPrompt);
          }
          setMonitorTarget(null);
          lastMonitorStateRef.current = '';
          lastMonitorHeartbeatAtRef.current = 0;
        }
      } catch (_e) {
        // Ignore transient poll errors.
      }
    };

    checkStatus();
    const timer = setInterval(checkStatus, 30000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [monitorTarget, awsAccessKey, awsSecretKey, awsRegion]);

  useEffect(() => {
    if (!serviceMonitorTarget) return undefined;
    if (!awsAccessKey || !awsSecretKey) return undefined;

    let cancelled = false;

    const checkStatus = async () => {
      try {
        const data = await fetchJson(`${API_BASE_URL}/resource/status`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            resource_type: serviceMonitorTarget.resourceType,
            resource_name: serviceMonitorTarget.resourceName,
            aws_access_key: awsAccessKey,
            aws_secret_key: awsSecretKey,
            aws_region: serviceMonitorTarget.region || awsRegion,
          }),
        });
        if (!data?.success || cancelled) {
          return;
        }

        const ctx = {
          resourceType: serviceMonitorTarget.resourceType,
          resourceName: serviceMonitorTarget.resourceName,
          region: serviceMonitorTarget.region || awsRegion,
          state: data.state,
          ready: data.ready,
        };
        setLastServiceStatusContext(ctx);
        setServiceStatusByType((prev) => ({ ...prev, [ctx.resourceType]: ctx }));

        const stateKey = `${ctx.resourceType}:${ctx.resourceName}:${data.state}:${data.ready}`;
        const now = Date.now();
        const heartbeatDue = now - lastServiceHeartbeatAtRef.current >= 90000;
        if (stateKey !== lastServiceStateRef.current) {
          lastServiceStateRef.current = stateKey;
          lastServiceHeartbeatAtRef.current = now;
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `Live status for ${ctx.resourceType} ${ctx.resourceName}: state=${data.state}, ready=${data.ready ? 'yes' : 'no'}.`,
          });
        } else if (!data.ready && heartbeatDue) {
          lastServiceHeartbeatAtRef.current = now;
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `Still waiting on ${ctx.resourceType} ${ctx.resourceName}: state=${data.state}. I will keep monitoring every 30s.`,
          });
        }

        if (data.ready && !cancelled) {
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `${ctx.resourceType.toUpperCase()} resource ${ctx.resourceName} is ready.`,
          });
          setServiceMonitorTarget(null);
          lastServiceStateRef.current = '';
          lastServiceHeartbeatAtRef.current = 0;
        }
      } catch (_e) {
        // Ignore transient poll errors.
      }
    };

    checkStatus();
    const timer = setInterval(checkStatus, 30000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceMonitorTarget, awsAccessKey, awsSecretKey, awsRegion]);

  const appendMessage = (msg) => {
    setMessages((prev) => [...prev, { ...msg, id: makeId() }]);
  };

  const executeAutoDeploy = async (payload) => {
    setLoading(true);
    try {
      const data = await fetchJson(`${API_BASE_URL}/ec2/deploy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        timeoutMs: EXECUTION_TIMEOUT_MS,
        body: JSON.stringify(payload),
      });
      appendMessage({
        role: 'assistant',
        kind: 'result',
        payload: {
          status: data?.success ? 'completed' : 'failed',
          intent: {
            action: 'deploy',
            resource_type: 'ec2',
            resource_name: payload.instance_id,
            region: payload.aws_region,
          },
          execution_result: data,
        },
      });
      if (data?.requires_input && Array.isArray(data?.questions) && data.questions.length > 0) {
        setPendingQuestions(data.questions);
        setPendingContinuation(data?.continuation || null);
        const cont = data?.continuation || null;
        if (cont?.kind === 'auto_remediation' && cont?.run_id && cont?.request_id) {
          setActiveRemediation({
            runId: cont.run_id,
            requestId: cont.request_id,
          });
        }
      }
      await loadDeployments();
    } catch (error) {
      appendMessage({
        role: 'assistant',
        kind: 'text',
        text: `Automatic deployment failed: ${error.message}`,
      });
    } finally {
      setLoading(false);
    }
  };

  const checkRemediationStatusNow = async (requestId, runId) => {
    if (!requestId || !runId) {
      throw new Error('Missing remediation request/run identifiers.');
    }
    return fetchJson(`${API_BASE_URL}/remediation/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_id: requestId,
        run_id: runId,
      }),
    });
  };

  const executeRemediationRun = async ({ requestId, runId, approved, note }) => {
    if (!requestId || !runId) {
      appendMessage({
        role: 'assistant',
        kind: 'text',
        text: 'Remediation identifiers are missing. Please rerun the deployment step.',
      });
      return;
    }
    setLoading(true);
    try {
      const data = await fetchJson(`${API_BASE_URL}/remediation/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        timeoutMs: EXECUTION_TIMEOUT_MS,
        body: JSON.stringify({
          request_id: requestId,
          run_id: runId,
          approved: Boolean(approved),
          note: note || '',
        }),
      });
      if (data?.result?.execution_result) {
        appendMessage({
          role: 'assistant',
          kind: 'result',
          payload: {
            status: data?.success ? 'completed' : 'failed',
            intent: data?.result?.intent || {},
            execution_result: data?.result?.execution_result || {},
          },
        });
        setLastResumeContext(data?.result?.execution_result?.resume_context || null);
      } else {
        appendMessage({
          role: 'assistant',
          kind: 'text',
          text: data?.message || (data?.success ? 'Remediation completed.' : 'Remediation failed.'),
        });
      }
      if (data?.status === 'completed' || data?.status === 'failed' || data?.status === 'denied') {
        setActiveRemediation(null);
      }
      await loadDeployments();
    } catch (error) {
      appendMessage({
        role: 'assistant',
        kind: 'text',
        text: `Remediation execution failed: ${error.message}`,
      });
    } finally {
      setLoading(false);
    }
  };

  const checkInstanceStatusNow = async (instanceId, regionHint) => {
    if (!instanceId) {
      throw new Error('No instance id found to check status.');
    }
    if (!awsAccessKey || !awsSecretKey) {
      throw new Error('AWS credentials are missing. Add Access Key and Secret Key first.');
    }
    return fetchJson(`${API_BASE_URL}/ec2/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        instance_id: instanceId,
        aws_access_key: awsAccessKey,
        aws_secret_key: awsSecretKey,
        aws_region: regionHint || awsRegion,
      }),
    });
  };

  const checkResourceStatusNow = async (resourceType, resourceName, regionHint) => {
    if (!resourceType || !resourceName) {
      throw new Error('resource type and name are required for status check.');
    }
    if (!awsAccessKey || !awsSecretKey) {
      throw new Error('AWS credentials are missing. Add Access Key and Secret Key first.');
    }
    return fetchJson(`${API_BASE_URL}/resource/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        resource_type: resourceType,
        resource_name: resourceName,
        aws_access_key: awsAccessKey,
        aws_secret_key: awsSecretKey,
        aws_region: regionHint || awsRegion,
      }),
    });
  };

  const resolveServiceStatusTarget = (hintedType) => {
    if (hintedType && hintedType !== 'ec2') {
      if (serviceMonitorTarget?.resourceType === hintedType) return serviceMonitorTarget;
      if (serviceStatusByType[hintedType]) return serviceStatusByType[hintedType];
      return null;
    }
    return serviceMonitorTarget || lastServiceStatusContext || null;
  };

  const handleStatusQuery = async (text) => {
    if (!looksLikeStatusQuery(text)) {
      return false;
    }

    if (activeRemediation?.requestId && activeRemediation?.runId) {
      try {
        const preview = await checkRemediationStatusNow(activeRemediation.requestId, activeRemediation.runId);
        const run = preview?.run || {};
        const plan = run?.plan || {};
        appendMessage({
          role: 'assistant',
          kind: 'text',
          text: `Remediation ${run?.run_id || '-'} status: ${run?.status || 'unknown'}. ${plan?.reason || ''}`.trim(),
        });
        if (run?.status === 'pending_approval') {
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: 'I am waiting for your approval to apply safe auto-remediation. Reply "approve" or "deny".',
          });
        }
        if (['completed', 'failed', 'denied', 'expired'].includes(String(run?.status || '').toLowerCase())) {
          setActiveRemediation(null);
        }
        return true;
      } catch (_err) {
        // Fall through to regular status handling.
      }
    }

    const hintedType = inferResourceTypeHint(text);
    const requestedInstanceId = extractInstanceId(text);
    const hasEc2Context = Boolean(requestedInstanceId || monitorTarget || lastAutoDeployContext || monitorSnapshot);
    const shouldTryEc2 = hintedType === 'ec2' || requestedInstanceId || (!hintedType && hasEc2Context);

    if (pendingPromptReview && (hasEc2Context || serviceMonitorTarget || lastServiceStatusContext)) {
      setPendingPromptReview(null);
    }

    if (shouldTryEc2) {
      const targetInstanceId =
        requestedInstanceId ||
        monitorTarget?.instanceId ||
        lastAutoDeployContext?.instanceId ||
        monitorSnapshot?.instance_id ||
        null;
      const targetRegion = monitorTarget?.region || lastAutoDeployContext?.region || monitorSnapshot?.region || awsRegion;

      if (!targetInstanceId) {
        appendMessage({
          role: 'assistant',
          kind: 'text',
          text: 'No active EC2 monitor found. Share an instance ID like i-1234abcd and I will check live status.',
        });
        return true;
      }

      try {
        const data = await checkInstanceStatusNow(targetInstanceId, targetRegion);
        const deployPayload = monitorTarget?.deploymentPayload || lastAutoDeployContext?.deploymentPayload || null;
        setMonitorSnapshot(data);
        appendMessage({
          role: 'assistant',
          kind: 'text',
          text: `Live update for ${data.instance_id}: state=${data.state}, instance=${data.instance_status}, system=${data.system_status}.`,
        });

        if (!data.ready) {
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: deployPayload
              ? 'Instance is not ready yet. I will continue monitoring and deploy automatically once checks pass.'
              : 'Instance is not ready yet. I will continue monitoring and move to deployment after readiness checks pass.',
          });
          return true;
        }

        if (deployPayload) {
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `Instance ${data.instance_id} is ready. Starting automatic deployment now.`,
          });
          await executeAutoDeploy(deployPayload);
          setMonitorTarget(null);
          lastMonitorStateRef.current = '';
          lastMonitorHeartbeatAtRef.current = 0;
        } else {
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `Instance ${data.instance_id} is ready. I need app details to continue deployment.`,
          });
        }
        return true;
      } catch (error) {
        appendMessage({
          role: 'assistant',
          kind: 'text',
          text: `Could not fetch live status: ${error.message}`,
        });
        return true;
      }
    }

    const target = resolveServiceStatusTarget(hintedType);
    if (!target) {
      const unfinished = await resolveLatestUnfinishedDeployment();
      if (unfinished?.request_id) {
        const history = await loadDeploymentHistory(unfinished.request_id);
        const latest = getLatestHistoryEntry(history) || unfinished;
        const summary = getSummary(latest);
        const resourceType = String(latest?.resource_type || summary?.resource || '').toLowerCase();
        const resourceName = String(latest?.resource_name || '').trim();
        if (resourceType && resourceName && (!hintedType || hintedType === resourceType)) {
          const recovered = {
            resourceType,
            resourceName,
            region: latest?.region || awsRegion,
          };
          setLastServiceStatusContext(recovered);
          setServiceStatusByType((prev) => ({ ...prev, [resourceType]: recovered }));
          setServiceMonitorTarget(recovered);
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `Recovered pending resource from deployment history: ${resourceType} ${resourceName}. Fetching live status now.`,
          });
          return await handleStatusQuery(`status ${resourceType} ${resourceName}`);
        }
      }
      appendMessage({
        role: 'assistant',
        kind: 'text',
        text: 'No tracked long-running resource found. Mention the service name and resource ID/name, then I can fetch live status.',
      });
      return true;
    }

    try {
      const data = await checkResourceStatusNow(target.resourceType, target.resourceName, target.region);
      const ctx = {
        resourceType: target.resourceType,
        resourceName: target.resourceName,
        region: target.region || awsRegion,
        state: data.state,
        ready: data.ready,
      };
      setLastServiceStatusContext(ctx);
      setServiceStatusByType((prev) => ({ ...prev, [ctx.resourceType]: ctx }));
      appendMessage({
        role: 'assistant',
        kind: 'text',
        text: `Live update for ${ctx.resourceType} ${ctx.resourceName}: state=${data.state}, ready=${data.ready ? 'yes' : 'no'}.`,
      });

      if (!data.ready) {
        setServiceMonitorTarget({
          resourceType: ctx.resourceType,
          resourceName: ctx.resourceName,
          region: ctx.region,
        });
        appendMessage({
          role: 'assistant',
          kind: 'text',
          text: `I will keep monitoring ${ctx.resourceType} ${ctx.resourceName} every 30s until it is ready.`,
        });
      } else if (
        serviceMonitorTarget?.resourceType === ctx.resourceType &&
        serviceMonitorTarget?.resourceName === ctx.resourceName
      ) {
        setServiceMonitorTarget(null);
        lastServiceStateRef.current = '';
        lastServiceHeartbeatAtRef.current = 0;
      }
      return true;
    } catch (error) {
      appendMessage({
        role: 'assistant',
        kind: 'text',
        text: `Could not fetch live status: ${error.message}`,
      });
      return true;
    }
  };

  const requestPromptImprovement = async (requestText) => {
    return fetchJson(`${API_BASE_URL}/prompt/improve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        natural_language_request: requestText,
        environment,
        aws_region: awsRegion,
      }),
    });
  };

  const executeRequest = async (requestText, variables = {}) => {
    const remembered = requestVariableMemory[requestText] || {};
    const mergedVariables = { ...remembered, ...variables };
    setLoading(true);
    try {
      const data = await fetchJson(`${API_BASE_URL}/requests`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        timeoutMs: EXECUTION_TIMEOUT_MS,
        body: JSON.stringify({
          natural_language_request: requestText,
          environment,
          cloud_provider: 'aws',
          aws_access_key: awsAccessKey,
          aws_secret_key: awsSecretKey,
          aws_region: awsRegion,
          input_variables: mergedVariables,
        }),
      });

      appendMessage({ role: 'assistant', kind: 'result', payload: data });
      if (data?.request_id) {
        setLastRequestId(data.request_id);
        setSelectedExistingRequestId(data.request_id);
        setSessionMode('existing');
      } else {
        setLastRequestId(null);
      }
      setLastResumeContext(data?.execution_result?.resume_context || null);
      await loadDeployments();

      const intent = data?.intent || {};
      const execResult = data?.execution_result || {};
      const resourceType = String(intent?.resource_type || execResult?.resource_type || '').toLowerCase();
      if (NON_EC2_STATUS_TYPES.has(resourceType)) {
        const resourceName = extractResourceNameFromResult(resourceType, intent, execResult);
        if (resourceName) {
          const serviceCtx = {
            resourceType,
            resourceName,
            region: intent?.region || awsRegion,
            state: execResult?.status || execResult?.state || null,
            ready: false,
          };
          setLastServiceStatusContext(serviceCtx);
          setServiceStatusByType((prev) => ({ ...prev, [resourceType]: serviceCtx }));

          if (inferNeedsPolling(resourceType, execResult)) {
            setServiceMonitorTarget(serviceCtx);
            appendMessage({
              role: 'assistant',
              kind: 'text',
              text: `I will monitor ${resourceType} ${resourceName} every 30 seconds and update you until it becomes ready.`,
            });
          } else if (
            serviceMonitorTarget?.resourceType === resourceType &&
            serviceMonitorTarget?.resourceName === resourceName
          ) {
            setServiceMonitorTarget(null);
            lastServiceStateRef.current = '';
            lastServiceHeartbeatAtRef.current = 0;
          }
        }
      }

      const questions = data?.execution_result?.questions || [];
      if (data.status === 'needs_input' && questions.length > 0) {
        setPendingQuestions(questions);
        setPendingContinuation(data?.execution_result?.continuation || null);
        const cont = data?.execution_result?.continuation || null;
        if (cont?.kind === 'auto_remediation' && cont?.run_id && (cont?.request_id || data?.request_id)) {
          setActiveRemediation({
            runId: cont.run_id,
            requestId: cont.request_id || data?.request_id,
          });
        }
        if (cont?.kind === 'auto_deploy_ssm' && cont?.recommended_wait_seconds > 0 && cont?.instance_id) {
          setLastAutoDeployContext({
            instanceId: cont.instance_id,
            region: cont.region || awsRegion,
            deploymentPayload: null,
          });
          setMonitorTarget({
            instanceId: cont.instance_id,
            region: cont.region || awsRegion,
            nextPrompt: null,
            deploymentPayload: null,
          });
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `I will monitor instance ${cont.instance_id} every 30 seconds and update you when it is ready.`,
          });
        }
        const q = questions[0];
        appendMessage({
          role: 'assistant',
          kind: 'question',
          text: q.question,
          meta: q,
        });
      } else {
        setPendingQuestions([]);
        setInputVariables({});
        setPendingContinuation(null);
        setMonitorTarget(null);
        setLastAutoDeployContext(null);
        setActiveRemediation(null);
      }
    } catch (error) {
      appendMessage({
        role: 'assistant',
        kind: 'text',
        text: `Execution failed: ${error.message || 'Request failed'}`,
      });
      await loadDeployments();
    } finally {
      setLoading(false);
    }
  };

  const startPromptReview = async (rawText) => {
    try {
      const review = await requestPromptImprovement(rawText);
      setPendingPromptReview(review);
      appendMessage({
        role: 'assistant',
        kind: 'prompt_review',
        payload: review,
      });
    } catch (error) {
      appendMessage({
        role: 'assistant',
        kind: 'text',
        text: `Could not improve prompt. Executing original request directly. (${error.message})`,
      });
      setActiveRequest(rawText);
      setInputVariables({});
      await executeRequest(rawText, {});
    }
  };

  const approveImprovedPrompt = async (review) => {
    setPendingPromptReview(null);
    setPendingQuestions([]);
    setInputVariables({});
    setActiveRequest(review.improved_prompt);
    appendMessage({
      role: 'user',
      kind: 'text',
      text: `Approved improved prompt: ${review.improved_prompt}`,
    });
    await executeRequest(review.improved_prompt, {});
  };

  const useImprovedAsDraft = (review) => {
    setPendingPromptReview(null);
    setComposer(review.improved_prompt || '');
    appendMessage({
      role: 'assistant',
      kind: 'text',
      text: 'I loaded the improved prompt into the input box. You can edit and send.',
    });
  };

  const runOriginalPrompt = async (review) => {
    const original = review.original_prompt || '';
    setPendingPromptReview(null);
    setPendingQuestions([]);
    setInputVariables({});
    setActiveRequest(original);
    appendMessage({
      role: 'user',
      kind: 'text',
      text: `Run original prompt: ${original}`,
    });
    await executeRequest(original, {});
  };

  const handleRemediationDecision = async (executionResult, approved) => {
    const continuation = executionResult?.continuation || {};
    const runId = continuation?.run_id || activeRemediation?.runId;
    const requestId = continuation?.request_id || lastRequestId || activeRemediation?.requestId;
    appendMessage({
      role: 'user',
      kind: 'text',
      text: approved ? 'approve' : 'deny',
    });
    await executeRemediationRun({
      requestId,
      runId,
      approved: Boolean(approved),
    });
    setPendingQuestions([]);
    setPendingContinuation(null);
    setInputVariables({});
  };

  const handleSend = async (e) => {
    e.preventDefault();
    if (!sessionReady) return;
    const text = composer.trim();
    if (!text || loading) return;

    appendMessage({ role: 'user', kind: 'text', text });
    setComposer('');

    if (pendingQuestion) {
      let answerValue = parseValue(text, pendingQuestion.type);
      const optionValues = Array.isArray(pendingQuestion.options)
        ? pendingQuestion.options.map((opt) => String(opt).toLowerCase().trim())
        : [];
      const rawAnswer = String(answerValue || '').toLowerCase().trim();
      if (
        isContinueCommand(rawAnswer) &&
        optionValues.length > 0 &&
        optionValues.includes('update') &&
        ['existing_operation', 'custom_operation'].includes(String(pendingQuestion.variable || '').toLowerCase())
      ) {
        answerValue = 'update';
      } else if (
        isContinueCommand(rawAnswer) &&
        optionValues.length > 0 &&
        optionValues.includes('custom') &&
        ['existing_operation', 'custom_operation'].includes(String(pendingQuestion.variable || '').toLowerCase())
      ) {
        answerValue = 'custom';
      }
      const merged = { ...inputVariables, [pendingQuestion.variable]: answerValue };
      setInputVariables(merged);
      if (activeRequest) {
        setRequestVariableMemory((prev) => ({
          ...prev,
          [activeRequest]: { ...(prev[activeRequest] || {}), [pendingQuestion.variable]: answerValue },
        }));
      }

      const rest = pendingQuestions.slice(1);
      setPendingQuestions(rest);

      if (rest.length > 0) {
        appendMessage({
          role: 'assistant',
          kind: 'question',
          text: rest[0].question,
          meta: rest[0],
        });
        return;
      }

      if (pendingContinuation?.kind === 'auto_remediation') {
        const answer = String(merged.remediation_approval || '').trim().toLowerCase();
        const approved = ['approve', 'approved', 'yes', 'y', 'true'].includes(answer);
        const denied = ['deny', 'denied', 'no', 'n', 'false'].includes(answer);
        if (!approved && !denied) {
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: 'Please answer with approve or deny.',
          });
          return;
        }
        await executeRemediationRun({
          requestId: pendingContinuation.request_id || lastRequestId || activeRemediation?.requestId,
          runId: pendingContinuation.run_id || activeRemediation?.runId,
          approved,
        });
        setPendingQuestions([]);
        setInputVariables({});
        setPendingContinuation(null);
        return;
      }

      if (pendingContinuation?.kind === 'auto_deploy_ssm') {
        const appTargets = toStringList(merged.app_targets || merged.app_name || 'tomcat');
        const customCommands = toStringList(merged.custom_commands || '');
        const deployPayload = {
          instance_id: pendingContinuation.instance_id,
          aws_access_key: awsAccessKey,
          aws_secret_key: awsSecretKey,
          aws_region: pendingContinuation.region || awsRegion,
          app_targets: appTargets,
          app_port: Number(merged.app_port || 8080),
          public_access: Boolean(merged.public_access ?? true),
          custom_commands: customCommands,
          wait_seconds: Number(pendingContinuation.recommended_wait_seconds || 300),
          request_id: lastRequestId,
          environment,
          request_text: activeRequest,
        };
        const waitSeconds = Number(pendingContinuation.recommended_wait_seconds || 0);
        if (waitSeconds > 0) {
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `Instance is still initializing. I will keep checking every 30s and auto-deploy once ready (about ${Math.ceil(waitSeconds / 60)} minutes).`,
          });
          if (pendingContinuation.instance_id) {
            setLastAutoDeployContext({
              instanceId: pendingContinuation.instance_id,
              region: pendingContinuation.region || awsRegion,
              deploymentPayload: deployPayload,
            });
            setMonitorTarget({
              instanceId: pendingContinuation.instance_id,
              region: pendingContinuation.region || awsRegion,
              nextPrompt: null,
              deploymentPayload: deployPayload,
            });
          }
        } else {
          setLastAutoDeployContext({
            instanceId: pendingContinuation.instance_id,
            region: pendingContinuation.region || awsRegion,
            deploymentPayload: deployPayload,
          });
          await executeAutoDeploy(deployPayload);
        }
        setPendingQuestions([]);
        setInputVariables({});
        setPendingContinuation(null);
        return;
      }

      await executeRequest(activeRequest, merged);
      return;
    }

    if (isContinueCommand(text)) {
      if (activeRequest) {
        appendMessage({
          role: 'assistant',
          kind: 'text',
          text: 'Continuing the current deployment flow.',
        });
        const remembered = requestVariableMemory[activeRequest] || {};
        await executeRequest(activeRequest, remembered);
        return;
      }
      if (lastResumeContext) {
        appendMessage({
          role: 'assistant',
          kind: 'text',
          text: 'Resuming from the latest deployment context.',
        });
        await executeRequest('resume latest deployment', { resume_skipped_only: true, resume_context: lastResumeContext });
        return;
      }
      const unfinished = await resolveLatestUnfinishedDeployment();
      if (unfinished?.request_id) {
        const history = await loadDeploymentHistory(unfinished.request_id);
        const latest = getLatestHistoryEntry(history) || unfinished;
        const summary = getSummary(latest);
        const resumeContext = summary?.resume_context && typeof summary.resume_context === 'object' ? summary.resume_context : null;
        const requestText =
          String(latest?.request_text || '').trim() ||
          String(summary?.request_text || '').trim() ||
          'continue latest deployment';
        setLastRequestId(String(unfinished.request_id));
        setSelectedExistingRequestId(String(unfinished.request_id));
        setSessionMode('existing');
        setActiveRequest(requestText);
        if (resumeContext) {
          setLastResumeContext(resumeContext);
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `Continuing unfinished deployment ${unfinished.request_id} from saved execution context.`,
          });
          await executeRequest(requestText, { resume_skipped_only: true, resume_context: resumeContext });
          return;
        }

        const resourceType = String(latest?.resource_type || summary?.resource || '').toLowerCase();
        const resourceName = String(latest?.resource_name || '').trim();
        if (resourceType && resourceName) {
          if (resourceType === 'ec2') {
            setMonitorTarget({
              instanceId: resourceName,
              region: latest?.region || awsRegion,
              nextPrompt: null,
              deploymentPayload: null,
            });
            appendMessage({
              role: 'assistant',
              kind: 'text',
              text: `Resumed monitoring EC2 instance ${resourceName}. I will continue automatically when it is ready.`,
            });
            await handleStatusQuery(`status ${resourceName}`);
            return;
          }
          const serviceCtx = {
            resourceType,
            resourceName,
            region: latest?.region || awsRegion,
          };
          setLastServiceStatusContext(serviceCtx);
          setServiceStatusByType((prev) => ({ ...prev, [resourceType]: serviceCtx }));
          setServiceMonitorTarget(serviceCtx);
          appendMessage({
            role: 'assistant',
            kind: 'text',
            text: `Resumed monitoring ${resourceType} ${resourceName}.`,
          });
          await handleStatusQuery(`status ${resourceType} ${resourceName}`);
          return;
        }
      }
    }

    const strategyPref = parseResourceStrategyPreference(text);
    if (strategyPref) {
      if (!activeRequest) {
        const unfinished = await resolveLatestUnfinishedDeployment();
        if (unfinished?.request_id) {
          const history = await loadDeploymentHistory(unfinished.request_id);
          const latest = getLatestHistoryEntry(history) || unfinished;
          const summary = getSummary(latest);
          const requestText =
            String(latest?.request_text || '').trim() ||
            String(summary?.request_text || '').trim() ||
            '';
          if (requestText) {
            appendMessage({
              role: 'assistant',
              kind: 'text',
              text: `Applying resource strategy "${strategyPref}" to unfinished deployment ${unfinished.request_id} and continuing.`,
            });
            setActiveRequest(requestText);
            setLastRequestId(String(unfinished.request_id));
            setSelectedExistingRequestId(String(unfinished.request_id));
            setSessionMode('existing');
            const resumeContext =
              summary?.resume_context && typeof summary.resume_context === 'object'
                ? summary.resume_context
                : null;
            if (resumeContext) setLastResumeContext(resumeContext);
            await executeRequest(requestText, {
              resource_strategy: strategyPref,
              ...(resumeContext ? { resume_skipped_only: true, resume_context: resumeContext } : {}),
            });
            return;
          }
        }
        appendMessage({
          role: 'assistant',
          kind: 'text',
          text: 'I need either a deployment request or an unfinished deployment record to apply that preference.',
        });
        return;
      }
      appendMessage({
        role: 'assistant',
        kind: 'text',
        text: `Applying resource strategy "${strategyPref}" to your current request and continuing.`,
      });
      setRequestVariableMemory((prev) => ({
        ...prev,
        [activeRequest]: { ...(prev[activeRequest] || {}), resource_strategy: strategyPref },
      }));
      await executeRequest(activeRequest, { resource_strategy: strategyPref });
      return;
    }

    const statusHandled = await handleStatusQuery(text);
    if (statusHandled) {
      return;
    }

    if (pendingPromptReview) {
      setPendingPromptReview(null);
      setActiveRequest(text);
      setPendingQuestions([]);
      setInputVariables({});
      await executeRequest(text, {});
      return;
    }

    const resumeKeywords = /(run|resume).*(skipped|failed).*(stage|phase)|skipped stage|resume/i;
    if (resumeKeywords.test(text) && lastResumeContext) {
      setActiveRequest(text);
      setPendingQuestions([]);
      setInputVariables({});
      appendMessage({
        role: 'assistant',
        kind: 'text',
        text: 'Resuming only the skipped stage from previous action.',
      });
      await executeRequest(text, { resume_skipped_only: true, resume_context: lastResumeContext });
      return;
    }

    await startPromptReview(text);
  };

  const useSuggestion = (text) => {
    setComposer(text);
  };

  const openMlflowUi = () => {
    const url = mlflowInfo?.ui_url || DEFAULT_MLFLOW_UI_URL;
    window.open(url, '_blank', 'noopener,noreferrer');
  };

  return (
    <div className="workspace-layout">
      <aside className="deploy-panel">
        <div className="deploy-panel-header">
          <div>
            <h3>My Deployments</h3>
            <p>{deploymentsLoading ? 'Refreshing...' : `${deployments.length} records`}</p>
          </div>
          <button type="button" onClick={loadDeployments} disabled={deploymentsLoading}>
            Refresh
          </button>
        </div>
        <DeploymentTable items={deployments} />
      </aside>

      <main className="chat-app full">
        <header className="chat-header">
          <div>
            <h1>Infra Execution Agent</h1>
            <p>User: {user?.username} ({user?.role})</p>
          </div>
          <div className="header-actions">
            {user?.role === 'admin' && (
              <button type="button" onClick={onGoAdmin}>Open Admin</button>
            )}
            {(mlflowInfo?.enabled || mlflowInfo?.available) && (
              <button type="button" onClick={openMlflowUi}>Open MLflow</button>
            )}
            <button type="button" onClick={onLogout}>Logout</button>
          </div>
        </header>

        <section className="control-bar">
          <div className="control-grid">
            <label>
              AWS Region
              <select value={awsRegion} onChange={(e) => setAwsRegion(e.target.value)}>
                {REGIONS.map((r) => (
                  <option key={r.value} value={r.value}>
                    {r.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Environment
              <select value={environment} onChange={(e) => setEnvironment(e.target.value)}>
                {ENVIRONMENTS.map((env) => (
                  <option key={env.value} value={env.value}>
                    {env.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              AWS Access Key
              <input
                type="text"
                value={awsAccessKey}
                onChange={(e) => setAwsAccessKey(e.target.value)}
                placeholder="AKIA..."
              />
            </label>
            <label>
              AWS Secret Key
              <input
                type="password"
                value={awsSecretKey}
                onChange={(e) => setAwsSecretKey(e.target.value)}
                placeholder="Secret key"
              />
            </label>
          </div>
        </section>

        <section className="chat-panel fixed">
          {showSessionChooser && (
            <div className="session-chooser-overlay">
              <div className="session-chooser-card">
                <h3>Choose Deployment Mode</h3>
                <p>Select how you want to continue.</p>
                <div className="session-chooser-actions">
                  <button type="button" onClick={startNewDeploymentSession} disabled={sessionRestoreBusy}>
                    New deployment
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      resumeExistingDeploymentSession(
                        selectedExistingRequestId || unfinishedDeployments[0]?.request_id || ''
                      )
                    }
                    disabled={sessionRestoreBusy || unfinishedDeployments.length === 0}
                  >
                    Existing deployment unfinished
                  </button>
                </div>
                <label>
                  Unfinished request
                  <select
                    value={selectedExistingRequestId}
                    onChange={(e) => setSelectedExistingRequestId(e.target.value)}
                    disabled={sessionRestoreBusy || unfinishedDeployments.length === 0}
                  >
                    {unfinishedDeployments.length === 0 && <option value="">No unfinished deployments found</option>}
                    {unfinishedDeployments.map((item) => (
                      <option key={item.request_id} value={item.request_id}>
                        {item.request_id} | {item.resource_type || '-'} | {item.status || '-'}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </div>
          )}

          <div className="suggestions">
            {SUGGESTIONS.map((s) => (
              <button key={s} type="button" onClick={() => useSuggestion(s)} disabled={!sessionReady}>
                {s}
              </button>
            ))}
          </div>

          <div className="message-list" ref={listRef}>
            {messages.map((msg) => (
              <div key={msg.id} className={`message-row ${msg.role}`}>
                <div className={`bubble ${msg.role}`}>
                  {msg.kind === 'result' ? (
                    <ResultMessage payload={msg.payload} onRemediationDecision={handleRemediationDecision} disabled={loading} />
                  ) : msg.kind === 'prompt_review' ? (
                    <PromptReviewMessage
                      payload={msg.payload}
                      onApprove={approveImprovedPrompt}
                      onUseDraft={useImprovedAsDraft}
                      onRunOriginal={runOriginalPrompt}
                      disabled={loading || !pendingPromptReview}
                    />
                  ) : (
                    <>
                      <div className="bubble-text">{msg.text}</div>
                      {msg.meta?.hint && <div className="bubble-hint">Hint: {msg.meta.hint}</div>}
                    </>
                  )}
                </div>
              </div>
            ))}

            {loading && (
              <div className="message-row assistant">
                <div className="bubble assistant working">
                  <LoadingTrace seconds={loadingSeconds} steps={executionSteps} activeStepIndex={activeStepIndex} />
                </div>
              </div>
            )}
          </div>

          <form className="composer" onSubmit={handleSend}>
            {pendingQuestion && (
              <div className="pending-banner">
                Waiting for: <strong>{pendingQuestion.variable}</strong>
              </div>
            )}
            {pendingQuestion?.options?.length > 0 && (
              <div className="quick-options">
                {pendingQuestion.options.map((opt) => (
                  <button
                    key={String(opt)}
                    type="button"
                    onClick={() => setComposer(String(opt))}
                    disabled={loading}
                  >
                    {String(opt)}
                  </button>
                ))}
              </div>
            )}
            {pendingPromptReview && !pendingQuestion && (
              <div className="pending-banner">
                Review the improved prompt above, approve it, or type your own edited prompt and send.
              </div>
            )}
            <div className="composer-row">
              <input
                value={composer}
                onChange={(e) => setComposer(e.target.value)}
                placeholder={
                  !sessionReady
                    ? 'Choose deployment mode to continue...'
                    : pendingQuestion
                    ? `Answer required input: ${pendingQuestion.variable}`
                    : pendingPromptReview
                      ? 'Edit improved prompt or type a new one...'
                      : 'Describe what you want in simple words...'
                }
                disabled={loading || !sessionReady}
              />
              <button type="submit" disabled={loading || !composer.trim() || !sessionReady}>
                Send
              </button>
            </div>
          </form>
        </section>
      </main>
    </div>
  );
}

function PromptReviewMessage({ payload, onApprove, onUseDraft, onRunOriginal, disabled }) {
  const phasePlan = Array.isArray(payload?.phase_plan) ? payload.phase_plan : [];
  return (
    <div className="prompt-review-card">
      <div className="result-line">
        <strong>Prompt Rewrite</strong>
      </div>
      <div className="bubble-text">{payload?.summary || 'I improved your prompt for clarity.'}</div>
      <div className="review-block">
        <div className="review-label">Original</div>
        <pre>{payload?.original_prompt || '-'}</pre>
      </div>
      <div className="review-block">
        <div className="review-label">Improved</div>
        <pre>{payload?.improved_prompt || '-'}</pre>
      </div>
      {phasePlan.length > 0 && (
        <div className="review-phases">
          {phasePlan.map((p) => (
            <span key={p.id} className="review-phase-chip">
              {p.title}
            </span>
          ))}
        </div>
      )}
      <div className="review-actions">
        <button type="button" onClick={() => onApprove(payload)} disabled={disabled}>
          Approve & Run
        </button>
        <button type="button" onClick={() => onUseDraft(payload)} disabled={disabled}>
          Use As Draft
        </button>
        <button type="button" onClick={() => onRunOriginal(payload)} disabled={disabled}>
          Run Original
        </button>
      </div>
    </div>
  );
}

function LoadingTrace({ seconds, steps, activeStepIndex }) {
  const cycleText = [
    'Designing execution plan...',
    'Preparing networking and policy checks...',
    'Provisioning compute/data resources...',
    'Deploying application changes...',
    'Validating health checks...',
  ];
  const liveText = cycleText[Math.floor(seconds / 2) % cycleText.length];
  const progressPercent = steps.length > 0 ? Math.max(8, Math.round(((activeStepIndex + 1) / steps.length) * 100)) : 10;

  return (
    <div className="trace-card">
      <div className="trace-head">
        <div className="trace-live">
          <span className="trace-dot" />
          <span>{liveText}</span>
        </div>
        <span className="trace-time">{seconds}s</span>
      </div>

      <div className="trace-track">
        <div className="trace-bar" style={{ width: `${progressPercent}%` }} />
      </div>

      <div className="trace-steps">
        {steps.map((step, idx) => {
          const state = idx < activeStepIndex ? 'done' : idx === activeStepIndex ? 'current' : 'pending';
          return (
            <div key={`${step}-${idx}`} className={`trace-step ${state}`}>
              <span className="trace-step-icon">{state === 'done' ? '✓' : state === 'current' ? '•' : '·'}</span>
              <span>{step}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ResultMessage({ payload, onRemediationDecision, disabled }) {
  const intent = payload?.intent || {};
  const exec = payload?.execution_result || {};
  const phases = Array.isArray(exec?.phases) ? exec.phases : [];
  const continuation = exec?.continuation || null;
  const remediation = exec?.remediation || null;
  const validation = exec?.outcome_validation || null;
  const validationSummary = validation?.summary || null;
  const status = payload?.status || 'failed';
  const ok = status === 'completed' || exec?.success;

  return (
    <div className="result-card">
      <div className={`status-pill ${ok ? 'ok' : status === 'needs_input' ? 'warn' : 'fail'}`}>
        {status === 'needs_input' ? 'Needs Input' : ok ? 'Completed' : 'Failed'}
      </div>

      <div className="result-line">
        <strong>Action:</strong> {intent.action || '-'} | <strong>Resource:</strong> {intent.resource_type || '-'} |{' '}
        <strong>Name:</strong> {intent.resource_name || '-'} | <strong>Region:</strong> {intent.region || '-'}
      </div>

      {exec?.error && <div className="error-line">{exec.error}</div>}
      {exec?.question_prompt && <div className="info-line">{exec.question_prompt}</div>}
      {exec?.final_outcome && <div className="info-line">{exec.final_outcome}</div>}
      {remediation?.required && (
        <div className="review-block">
          <div className="review-label">Auto Remediation</div>
          <div className="bubble-text">{remediation.reason || 'Auto-remediation is available for this failure.'}</div>
          {Array.isArray(remediation.actions) && remediation.actions.length > 0 && (
            <div className="phase-grid">
              {remediation.actions.map((item, idx) => (
                <div key={`rem-${idx}`} className="phase-item in_progress">
                  <span>{item}</span>
                  <strong>planned</strong>
                </div>
              ))}
            </div>
          )}
          {Array.isArray(remediation.required_permissions) && remediation.required_permissions.length > 0 && (
            <div className="info-line">
              Required permissions: {remediation.required_permissions.join(', ')}
            </div>
          )}
          {continuation?.kind === 'auto_remediation' && (
            <div className="review-actions">
              <button type="button" onClick={() => onRemediationDecision(exec, true)} disabled={disabled}>
                Approve & Continue
              </button>
              <button type="button" onClick={() => onRemediationDecision(exec, false)} disabled={disabled}>
                Deny
              </button>
            </div>
          )}
        </div>
      )}
      {continuation?.recommended_wait_seconds > 0 && (
        <div className="info-line">
          Recommended wait: about {Math.ceil(continuation.recommended_wait_seconds / 60)} minutes before next deploy step.
        </div>
      )}
      {validationSummary && (
        <div className="info-line">
          Outcome checks: {validationSummary.passed}/{validationSummary.total} passed
          {validationSummary.failed > 0 ? `, ${validationSummary.failed} failed` : ''}.
        </div>
      )}
      {Array.isArray(validation?.checks) && validation.checks.length > 0 && (
        <div className="phase-grid">
          {validation.checks.map((check, idx) => (
            <div
              key={`${check.type || 'check'}-${idx}`}
              className={`phase-item ${
                check.state === 'pending'
                  ? 'in_progress'
                  : check.state === 'skipped'
                    ? 'pending'
                    : check.success
                      ? 'completed'
                      : 'failed'
              }`}
            >
              <span>{check.type || 'check'}</span>
              <strong>{check.state || (check.success ? 'passed' : 'failed')}</strong>
            </div>
          ))}
        </div>
      )}

      {phases.length > 0 && (
        <div className="phase-grid">
          {phases.map((phase) => (
            <div key={phase.id} className={`phase-item ${phase.status || 'pending'}`}>
              <span>{phase.title}</span>
              <strong>{phase.status || 'pending'}</strong>
            </div>
          ))}
        </div>
      )}

      <details>
        <summary>Execution Details</summary>
        <pre>{JSON.stringify(exec, null, 2)}</pre>
      </details>
    </div>
  );
}

export default ChatPage;
