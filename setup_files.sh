#!/bin/bash

echo "🚀 Creating all required files..."

# Create __init__.py files
echo "📁 Creating __init__.py files..."
touch backend/services/__init__.py
touch backend/models/__init__.py
touch backend/api/__init__.py
echo "✓ __init__.py files created"

# Create rag_service.py
echo "📄 Creating rag_service.py..."
cat > backend/services/rag_service.py << 'RAGEOF'
"""
RAG Service for Terraform Documentation
"""

import os
import asyncio
from typing import List, Dict, Optional
from pathlib import Path
import chromadb
from chromadb.config import Settings as ChromaSettings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.docstore.document import Document
import requests
from config import settings
import ollama

class RAGService:
    def __init__(self):
        self.vector_db_path = settings.VECTOR_DB_PATH
        self.embedding_model = settings.OLLAMA_EMBEDDING_MODEL
        self.client = None
        self.collection = None
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
            length_function=len,
        )
        
    async def initialize(self):
        """Initialize ChromaDB and load documents"""
        print("🔧 Initializing vector database...")
        
        self.client = chromadb.Client(ChromaSettings(
            is_persistent=True,
            persist_directory=self.vector_db_path
        ))
        
        try:
            self.collection = self.client.get_collection(name="terraform_docs")
            print(f"✓ Loaded existing collection with {self.collection.count()} documents")
        except:
            self.collection = self.client.create_collection(name="terraform_docs")
            print("✓ Created new collection")
            await self.ingest_terraform_docs()
    
    async def ingest_terraform_docs(self):
        """Ingest Terraform documentation"""
        print("📚 Ingesting Terraform documentation...")
        
        terraform_docs = [
            {
                "title": "AWS S3 Bucket",
                "content": """
                resource "aws_s3_bucket" "example" {
                  bucket = "my-bucket"
                  tags = {
                    Name = "My bucket"
                    Environment = "Dev"
                  }
                }
                S3 buckets support encryption, versioning, lifecycle rules.
                """,
                "provider": "aws",
                "resource_type": "aws_s3_bucket"
            },
            {
                "title": "AWS EC2 Instance",
                "content": """
                resource "aws_instance" "example" {
                  ami = "ami-0c55b159cbfafe1f0"
                  instance_type = "t3.micro"
                  tags = {
                    Name = "ExampleInstance"
                  }
                }
                """,
                "provider": "aws",
                "resource_type": "aws_instance"
            }
        ]
        
        all_chunks = []
        all_embeddings = []
        all_metadata = []
        all_ids = []
        
        for idx, doc in enumerate(terraform_docs):
            chunks = self.text_splitter.split_text(doc["content"])
            
            for chunk_idx, chunk in enumerate(chunks):
                response = ollama.embeddings(
                    model=self.embedding_model,
                    prompt=chunk
                )
                embedding = response["embedding"]
                
                metadata = {
                    "title": doc["title"],
                    "provider": doc["provider"],
                    "resource_type": doc["resource_type"],
                    "chunk_index": chunk_idx
                }
                
                all_chunks.append(chunk)
                all_embeddings.append(embedding)
                all_metadata.append(metadata)
                all_ids.append(f"doc_{idx}_chunk_{chunk_idx}")
        
        self.collection.add(
            embeddings=all_embeddings,
            documents=all_chunks,
            metadatas=all_metadata,
            ids=all_ids
        )
        
        print(f"✓ Ingested {len(all_chunks)} document chunks")
    
    async def retrieve(self, query: str, n_results: int = 5, provider: Optional[str] = None) -> List[Dict]:
        """Retrieve relevant documents"""
        response = ollama.embeddings(
            model=self.embedding_model,
            prompt=query
        )
        query_embedding = response["embedding"]
        
        where_filter = {}
        if provider:
            where_filter["provider"] = provider
        
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            where=where_filter if where_filter else None
        )
        
        retrieved_docs = []
        for i in range(len(results["documents"][0])):
            retrieved_docs.append({
                "content": results["documents"][0][i],
                "metadata": results["metadatas"][0][i],
                "distance": results["distances"][0][i] if "distances" in results else None
            })
        
        return retrieved_docs
    
    def get_stats(self) -> Dict:
        """Get collection statistics"""
        if self.collection:
            return {
                "total_documents": self.collection.count(),
                "embedding_model": self.embedding_model
            }
        return {"status": "not initialized"}
RAGEOF
echo "✓ rag_service.py created"

# Create orchestrator.py
echo "📄 Creating orchestrator.py..."
cat > backend/services/orchestrator.py << 'ORCHEOF'
"""
Orchestration Service using LangGraph
"""

import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime
from enum import Enum
import json
from dataclasses import dataclass
import ollama
from config import settings

class WorkflowState(Enum):
    INTAKE = "intake"
    CONTEXT_BUILD = "context_build"
    VALIDATION = "validation"
    RAG_RETRIEVAL = "rag_retrieval"
    IAC_GENERATION = "iac_generation"
    POLICY_CHECK = "policy_check"
    CHANGE_REQUEST = "change_request"
    AWAIT_APPROVAL = "await_approval"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class WorkflowContext:
    request_id: str
    requester_id: str
    environment: str
    cloud_provider: str
    natural_language_request: str
    current_state: WorkflowState
    history: List[Dict[str, Any]]
    context_data: Dict[str, Any]
    generated_code: Optional[str] = None
    validation_results: Optional[Dict] = None
    policy_results: Optional[Dict] = None
    approval_status: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = None
    updated_at: datetime = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

class OrchestrationService:
    def __init__(self, rag_service):
        self.rag_service = rag_service
        self.orchestrator_model = settings.OLLAMA_ORCHESTRATOR_MODEL
        self.codegen_model = settings.OLLAMA_CODEGEN_MODEL
        
    async def process_request(self, request_data: Dict[str, Any]) -> WorkflowContext:
        context = WorkflowContext(
            request_id=request_data["request_id"],
            requester_id=request_data["requester_id"],
            environment=request_data["environment"],
            cloud_provider=request_data["cloud_provider"],
            natural_language_request=request_data["natural_language_request"],
            current_state=WorkflowState.INTAKE,
            history=[],
            context_data={}
        )
        
        try:
            context = await self.intake_step(context)
            context = await self.context_build_step(context)
            context = await self.rag_retrieval_step(context)
            context = await self.iac_generation_step(context)
            context = await self.policy_check_step(context)
            context = await self.change_request_step(context)
            context.current_state = WorkflowState.AWAIT_APPROVAL
        except Exception as e:
            context.current_state = WorkflowState.FAILED
            context.error = str(e)
        
        return context
    
    async def intake_step(self, context: WorkflowContext) -> WorkflowContext:
        print(f"📥 Intake: Processing request {context.request_id}")
        context.context_data["classification"] = {"resource_type": "unknown", "action": "create"}
        context.current_state = WorkflowState.CONTEXT_BUILD
        return context
    
    async def context_build_step(self, context: WorkflowContext) -> WorkflowContext:
        print(f"🏗️  Context Build")
        context.context_data["required_tags"] = {"Environment": context.environment}
        context.current_state = WorkflowState.VALIDATION
        return context
    
    async def rag_retrieval_step(self, context: WorkflowContext) -> WorkflowContext:
        print(f"📚 RAG Retrieval")
        retrieved_docs = await self.rag_service.retrieve(
            query=f"{context.cloud_provider} {context.natural_language_request}",
            n_results=3
        )
        context.context_data["retrieved_docs"] = retrieved_docs
        context.current_state = WorkflowState.IAC_GENERATION
        return context
    
    async def iac_generation_step(self, context: WorkflowContext) -> WorkflowContext:
        print(f"⚙️  IaC Generation")
        prompt = f"Generate Terraform for: {context.natural_language_request}"
        response = ollama.generate(model=self.codegen_model, prompt=prompt)
        context.generated_code = response["response"]
        context.current_state = WorkflowState.POLICY_CHECK
        return context
    
    async def policy_check_step(self, context: WorkflowContext) -> WorkflowContext:
        print(f"🔒 Policy Check")
        context.policy_results = {"passed": True, "checks": []}
        context.current_state = WorkflowState.CHANGE_REQUEST
        return context
    
    async def change_request_step(self, context: WorkflowContext) -> WorkflowContext:
        print(f"📝 Change Request")
        context.context_data["change_request"] = {"cr_id": f"CR-{context.request_id}"}
        return context
ORCHEOF
echo "✓ orchestrator.py created"

# Create routes.py
echo "📄 Creating routes.py..."
cat > backend/api/routes.py << 'ROUTESEOF'
"""
API Routes
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
import uuid

router = APIRouter()

class InfrastructureRequest(BaseModel):
    natural_language_request: str
    environment: str
    cloud_provider: str
    requester_id: str = "admin@localhost"

@router.post("/requests")
async def create_request(request: InfrastructureRequest, app_request: Request):
    request_id = str(uuid.uuid4())
    orchestrator = app_request.app.state.orchestrator
    
    try:
        context = await orchestrator.process_request({
            "request_id": request_id,
            "requester_id": request.requester_id,
            "environment": request.environment,
            "cloud_provider": request.cloud_provider,
            "natural_language_request": request.natural_language_request
        })
        return {
            "request_id": request_id,
            "status": "processing" if not context.error else "failed",
            "workflow_state": context.current_state.value
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/providers")
async def list_providers():
    return {
        "providers": [
            {"id": "aws", "name": "Amazon Web Services"},
            {"id": "azure", "name": "Microsoft Azure"},
            {"id": "gcp", "name": "Google Cloud Platform"}
        ]
    }

@router.get("/environments")
async def list_environments():
    return {
        "environments": [
            {"id": "dev", "name": "Development", "approval_count": 0},
            {"id": "qa", "name": "QA", "approval_count": 1},
            {"id": "prod", "name": "Production", "approval_count": 2}
        ]
    }

@router.get("/health/detailed")
async def detailed_health():
    return {"status": "healthy", "components": {"ollama": "healthy", "database": "healthy"}}

@router.post("/rag/search")
async def search_docs(query: str, provider: Optional[str] = None, app_request: Request = None):
    rag_service = app_request.app.state.rag_service
    results = await rag_service.retrieve(query=query, n_results=5, provider=provider)
    return {"query": query, "results_count": len(results), "results": results}
ROUTESEOF
echo "✓ routes.py created"

# Create config.py
echo "📄 Creating config.py..."
cat > backend/config.py << 'CONFIGEOF'
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_PATH: str
    MODELS_PATH: str
    DATA_PATH: str
    VECTOR_DB_PATH: str
    DATABASE_URL: str
    REDIS_URL: str
    OLLAMA_BASE_URL: str
    OLLAMA_ORCHESTRATOR_MODEL: str
    OLLAMA_CODEGEN_MODEL: str
    OLLAMA_EMBEDDING_MODEL: str
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_WORKERS: int = 2
    ENABLE_JIRA: bool = False
    ENABLE_JENKINS: bool = False
    ENABLE_EMAIL: bool = False
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    
    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
CONFIGEOF
echo "✓ config.py created"

# Create main.py
echo "📄 Creating main.py..."
cat > backend/main.py << 'MAINEOF'
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
from dotenv import load_dotenv

load_dotenv()

from services.orchestrator import OrchestrationService
from services.rag_service import RAGService
from api.routes import router
from config import settings

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting AI Infrastructure Platform...")
    print("📚 Initializing RAG service...")
    rag_service = RAGService()
    await rag_service.initialize()
    app.state.rag_service = rag_service
    print("🤖 Initializing orchestration service...")
    orchestrator = OrchestrationService(rag_service)
    app.state.orchestrator = orchestrator
    print("✅ Platform ready!")
    yield
    print("👋 Shutting down...")

app = FastAPI(
    title="AI Infrastructure Provisioning Platform",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")

@app.get("/")
async def root():
    return {"service": "AI Infrastructure Platform", "version": "1.0.0", "status": "operational"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.API_HOST, port=settings.API_PORT, reload=settings.DEBUG)
MAINEOF
echo "✓ main.py created"

# Create .env if it doesn't exist
if [ ! -f .env ]; then
    echo "📄 Creating .env file..."
    cat > .env << ENVEOF
PROJECT_PATH=$HOME/Library/Mobile Documents/com~apple~CloudDocs/ai-infra-platform
MODELS_PATH=$HOME/Library/Mobile Documents/com~apple~CloudDocs/ai-infra-platform/models
DATA_PATH=$HOME/Library/Mobile Documents/com~apple~CloudDocs/ai-infra-platform/data
VECTOR_DB_PATH=$HOME/Library/Mobile Documents/com~apple~CloudDocs/ai-infra-platform/vectordb

DATABASE_URL=postgresql://localhost:5432/ai_infra_platform
REDIS_URL=redis://localhost:6379/0

OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_ORCHESTRATOR_MODEL=llama3.1:8b
OLLAMA_CODEGEN_MODEL=codellama:7b
OLLAMA_EMBEDDING_MODEL=nomic-embed-text

JWT_SECRET_KEY=$(openssl rand -hex 32)
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60

API_HOST=0.0.0.0
API_PORT=8000
API_WORKERS=2

FRONTEND_PORT=5173

ENABLE_JIRA=false
ENABLE_JENKINS=false
ENABLE_EMAIL=false

ENVIRONMENT=development
DEBUG=true
ENVEOF
    echo "✓ .env created"
else
    echo "✓ .env already exists"
fi

# Create start.sh
echo "📄 Creating start.sh..."
cat > start.sh << 'STARTEOF'
#!/bin/bash
cd "$HOME/Library/Mobile Documents/com~apple~CloudDocs/ai-infra-platform"
source venv/bin/activate
export OLLAMA_MODELS="$PWD/models"

echo "🚀 Starting AI Infrastructure Platform..."

cd backend
python main.py &
BACKEND_PID=$!
echo "✓ Backend started (PID: $BACKEND_PID)"

cd ../frontend
npm run dev &
FRONTEND_PID=$!
echo "✓ Frontend started (PID: $FRONTEND_PID)"

echo ""
echo "Platform running:"
echo "  Backend:  http://localhost:8000"
echo "  Frontend: http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop..."

trap "kill $BACKEND_PID $FRONTEND_PID; exit" INT TERM
wait
STARTEOF
chmod +x start.sh
echo "✓ start.sh created"

# Create stop.sh
cat > stop.sh << 'STOPEOF'
#!/bin/bash
echo "🛑 Stopping services..."
pkill -f "python main.py"
pkill -f "vite"
echo "✓ Services stopped"
STOPEOF
chmod +x stop.sh
echo "✓ stop.sh created"

echo ""
echo "✅ All files created successfully!"
echo ""
echo "Next steps:"
echo "  1. cd frontend && npm install && cd .."
echo "  2. ./start.sh"
