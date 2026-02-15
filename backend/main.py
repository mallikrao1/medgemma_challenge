from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import uvicorn

# Load .env from project root
from dotenv import load_dotenv
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

from services.orchestrator import OrchestrationService
from services.rag_service import RAGService
from services.auth_service import AuthService
from services.mlflow_tracker import MLflowTracker
from api.routes import router
from config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("  Starting AI Infrastructure Platform...")

    # Create RAG service but do NOT block on ChromaDB init
    rag_service = RAGService()
    app.state.rag_service = rag_service

    # Create orchestrator immediately (doesn't need RAG to be ready)
    orchestrator = OrchestrationService(rag_service)
    app.state.orchestrator = orchestrator
    auth_service = AuthService()
    auth_service.initialize()
    app.state.auth_service = auth_service
    mlflow_tracker = MLflowTracker(
        enabled=settings.MLFLOW_ENABLED,
        tracking_uri=settings.MLFLOW_TRACKING_URI,
        experiment_name=settings.MLFLOW_EXPERIMENT_NAME,
    )
    mlflow_init = mlflow_tracker.initialize()
    app.state.mlflow_tracker = mlflow_tracker
    if mlflow_init.get("success"):
        print(f"  MLflow tracking ready: {settings.MLFLOW_TRACKING_URI} (exp={settings.MLFLOW_EXPERIMENT_NAME})")
    else:
        print(f"  MLflow tracking unavailable: {mlflow_init.get('error')}")

    print("  Platform ready! (RAG initializing in background)")

    # Init RAG in background - doesn't block server startup
    asyncio.create_task(_init_rag_background(rag_service))

    yield
    print("  Shutting down...")


async def _init_rag_background(rag_service):
    """Initialize RAG in background so server starts fast."""
    try:
        await rag_service.initialize()
        print("  RAG service initialized (background)")
    except Exception as e:
        print(f"  RAG init failed (non-critical): {e}")


app = FastAPI(
    title="AI Infrastructure Provisioning Platform",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.get("/")
async def root():
    return {
        "service": "AI Infrastructure Platform",
        "version": "2.0.0",
        "status": "operational",
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.API_HOST, port=settings.API_PORT, reload=settings.DEBUG)
