import tempfile
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

import os
from dotenv import load_dotenv
from parse_legislation_codes import LegislationCodeParser
from pdf_to_markdown import convert_pdf_to_markdown

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

@app.post("/api/parse-legislation")
async def parse_legislation(file: UploadFile = File(...)):
    """
    Upload a PDF file, convert it to Markdown, parse legislation codes, and return structured JSON.
    """
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")
    
    try:
        # Create temporary files for PDF and Markdown
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_pdf:
            temp_pdf_path = temp_pdf.name
            temp_pdf.write(await file.read())
        
        temp_md_path = temp_pdf_path.replace('.pdf', '.md')
        
        # Convert PDF to Markdown
        convert_pdf_to_markdown(temp_pdf_path, temp_md_path)
        
        # Parse Markdown for legislation codes
        parser = LegislationCodeParser()
        result = parser.parse_markdown(temp_md_path)
        
        # Clean up temporary files
        os.unlink(temp_pdf_path)
        os.unlink(temp_md_path)
        
        return result
    except Exception as e:
        # Clean up on error
        if 'temp_pdf_path' in locals() and os.path.exists(temp_pdf_path):
            os.unlink(temp_pdf_path)
        if 'temp_md_path' in locals() and os.path.exists(temp_md_path):
            os.unlink(temp_md_path)
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)