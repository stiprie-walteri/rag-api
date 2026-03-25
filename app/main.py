import logging
import os
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.auth import ClerkAuthMiddleware
from app.api.dependencies import docstore_service
from app.api.routes import documents, folders, legislation, user

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Python API Template",
    description="A simple Python API template with health check endpoint",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(ClerkAuthMiddleware)

@app.on_event("startup")
def initialize_docstore() -> None:
    if docstore_service is None:
        return
    docstore_service.initialize()

app.include_router(user.router)
app.include_router(legislation.router)
app.include_router(documents.router)
app.include_router(folders.router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
