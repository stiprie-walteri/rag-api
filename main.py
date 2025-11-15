from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from openai import OpenAI
from typing import List, Optional
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Create FastAPI instance
app = FastAPI(
    title="Python API Template",
    description="A simple Python API template with health check endpoint",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Initialize OpenAI client for Featherless.ai
def get_featherless_client():
    api_key = os.getenv("FEATHERLESS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="FEATHERLESS_API_KEY not configured")
    return OpenAI(
        base_url="https://api.featherless.ai/v1",
        api_key=api_key
    )

# Response model for health check
class HealthResponse(BaseModel):
    status: str
    message: str

# Get default model from environment
def get_default_model():
    return os.getenv("FEATHERLESS_MODEL", "deepseek-ai/DeepSeek-V3-0324")

# Request/Response models for prompt endpoint
class PromptRequest(BaseModel):
    prompt: str
    model: Optional[str] = None

class PromptResponse(BaseModel):
    response: str
    model: str
    usage: dict

# Request/Response models for conversation endpoint
class Message(BaseModel):
    role: str
    content: str

class ConversationRequest(BaseModel):
    messages: List[Message]
    model: Optional[str] = None

class ConversationResponse(BaseModel):
    response: str
    model: str
    usage: dict

@app.get("/api/healthz", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint
    Returns the health status of the API
    """
    return HealthResponse(
        status="healthy",
        message="API is running successfully"
    )

@app.post("/api/prompt", response_model=PromptResponse)
async def prompt(request: PromptRequest):
    """
    Send a single prompt to Featherless.ai
    Returns the AI response
    """
    try:
        client = get_featherless_client()
        model = request.model or get_default_model()
        
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": request.prompt}
            ]
        )
        
        return PromptResponse(
            response=response.choices[0].message.content,
            model=response.model,
            usage=response.usage.model_dump()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error calling Featherless.ai: {str(e)}")

@app.post("/api/conversation", response_model=ConversationResponse)
async def conversation(request: ConversationRequest):
    """
    Have a conversation with the AI model
    Accepts a list of messages to maintain conversation context
    """
    try:
        client = get_featherless_client()
        model = request.model or get_default_model()
        
        # Convert Pydantic models to dicts for the API call
        messages = [{"role": msg.role, "content": msg.content} for msg in request.messages]
        
        response = client.chat.completions.create(
            model=model,
            messages=messages
        )
        
        return ConversationResponse(
            response=response.choices[0].message.content,
            model=response.model,
            usage=response.usage.model_dump()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error calling Featherless.ai: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)