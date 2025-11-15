from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

import os
from dotenv import load_dotenv
from parse_legislation_codes import LegislationCodeParser

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

# Response model for health check
class HealthResponse(BaseModel):
    status: str
    message: str

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

@app.get("/api/parse-legislation")
async def parse_legislation():
    """
    Parse legislation codes from org_submission.md
    Returns the structured JSON with extracted legislation codes
    """
    try:
        parser = LegislationCodeParser()
        result = parser.parse_markdown("org_submission.md")
        return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="org_submission.md file not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error parsing legislation codes: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)