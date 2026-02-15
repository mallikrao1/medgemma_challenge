from pathlib import Path
from pydantic_settings import BaseSettings

# Resolve .env path relative to this file (backend/) -> parent is project root
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    PROJECT_PATH: str = ""
    MODELS_PATH: str = ""
    DATA_PATH: str = ""
    VECTOR_DB_PATH: str = ""
    DATABASE_URL: str = "postgresql://user:password@db:5432/ai_infra"
    REDIS_URL: str = "redis://redis:6379/0"
    OLLAMA_BASE_URL: str = "http://ollama:11434"
    OLLAMA_ORCHESTRATOR_MODEL: str = "llama3.2:3b"
    OLLAMA_CODEGEN_MODEL: str = "llama3.2:3b"
    OLLAMA_EMBEDDING_MODEL: str = "nomic-embed-text"
    MODEL_ROUTER_PROFILE: str = "balanced"
    MODEL_ROUTER_ENABLE_SCORER: bool = True
    MODEL_ROUTER_SCORER_EXPLORATION: float = 0.15
    MODEL_ROUTER_QUALITY_WEIGHT: float = 0.65
    MODEL_ROUTER_LATENCY_WEIGHT: float = 0.35
    MODEL_ROUTER_FAILURE_WEIGHT: float = 0.50
    MODEL_ROUTER_EMA_ALPHA: float = 0.35
    NLU_PRIMARY_MODEL: str = "qwen2.5:14b-instruct"
    NLU_FALLBACK_MODELS: str = "llama3.1:8b-instruct,llama3.2:3b"
    CODEGEN_PRIMARY_MODEL: str = "qwen2.5-coder:14b"
    CODEGEN_FALLBACK_MODELS: str = "qwen2.5-coder:7b,llama3.2:3b"
    VERIFIER_MODEL: str = "qwen2.5:14b-instruct"
    VERIFIER_FALLBACK_MODELS: str = "llama3.1:8b-instruct,llama3.2:3b"
    GLM5_NLU_MODELS: str = "glm-5:cloud"
    GLM5_CODEGEN_MODELS: str = "glm-5:cloud"
    GLM5_VERIFIER_MODELS: str = "glm-5:cloud"
    NLU_ENABLE_VERIFIER: bool = True
    NLU_VERIFIER_MIN_CONFIDENCE: float = 0.55
    JWT_SECRET_KEY: str = "dev-secret-key"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    SESSION_DURATION_HOURS: int = 24
    SESSION_SLIDING_REFRESH: bool = True
    SESSION_REFRESH_WINDOW_MINUTES: int = 180
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_WORKERS: int = 2
    FRONTEND_PORT: int = 5173
    ENABLE_JIRA: bool = False
    ENABLE_JENKINS: bool = False
    ENABLE_EMAIL: bool = False
    OUTCOME_ADAPTER_RULES_PATH: str = ""
    REMEDIATION_RULES_PATH: str = ""
    AUTO_REMEDIATION_ENABLED: bool = True
    AUTO_REMEDIATION_ALL_SERVICES: bool = True
    AUTO_REMEDIATION_PREVIEW_ONLY: bool = False
    AUTO_REMEDIATION_MAX_ATTEMPTS: int = 2
    AUTO_REMEDIATION_AUTO_EXECUTE_SAFE: bool = True
    MLFLOW_ENABLED: bool = True
    MLFLOW_TRACKING_URI: str = "http://mlflow:5000"
    MLFLOW_EXPERIMENT_NAME: str = "ai-infra-agent"
    MLFLOW_UI_URL: str = "http://localhost:5001"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    class Config:
        env_file = str(ENV_FILE)
        case_sensitive = True


settings = Settings()
