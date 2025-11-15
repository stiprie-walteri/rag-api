import tempfile
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from parse_legislation_codes import LegislationCodeParser
from pdf_to_markdown import convert_pdf_to_markdown

# Import pipeline functions
from legislation_util.find_sections import load_legislation_unique_sections, parse_submission_codes, compute_metrics
from legislation_util.get_legislation_by_section import load_legislation, get_subsections_for_code
from get_submission_chunks import get_submission_by_codes
from compare_chunks import init_openai_from_env, call_openai_for_issues, IssueList, find_sections_for_code

# Load environment variables
load_dotenv()

# Load mock response if available
MOCK_RESPONSE_FILE = Path("full_response.json")
mock_response = None
if MOCK_RESPONSE_FILE.exists():
    try:
        with open(MOCK_RESPONSE_FILE, 'r', encoding='utf-8') as f:
            mock_response = json.load(f)
    except Exception as e:
        print(f"Warning: Failed to load mock response from {MOCK_RESPONSE_FILE}: {e}")

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

# Response model for parse-legislation
class ParseResponse(BaseModel):
    markdown: str
    parsed_codes: dict
    metrics: dict
    issues: dict

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

@app.post("/api/parse-legislation", response_model=ParseResponse)
async def parse_legislation(file: UploadFile = File(...)):
    """
    Upload a PDF file, run the full pipeline (PDF -> Markdown -> Parsed Codes -> Issues), and return all outputs.
    If MOCK_RESPONSE=true, return mock data immediately.
    """
    # Check for mock response
    if os.getenv("MOCK_RESPONSE", "false").lower() == "true":
        if mock_response:
            return ParseResponse(**mock_response)
        else:
            print("Warning: MOCK_RESPONSE=true but mock file not loaded; proceeding with real pipeline.")
    
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")
    
    temp_pdf_path = None
    temp_md_path = None
    temp_parsed_path = None
    
    try:
        # Step 1: Create temporary files
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_pdf:
            temp_pdf_path = temp_pdf.name
            temp_pdf.write(await file.read())
        
        temp_md_path = temp_pdf_path.replace('.pdf', '.md')
        
        # Step 2: Convert PDF to Markdown
        convert_pdf_to_markdown(temp_pdf_path, temp_md_path)
        
        # Read the full Markdown content
        with open(temp_md_path, 'r', encoding='utf-8') as f:
            full_markdown = f.read()
        
        # Step 3: Parse Markdown for legislation codes
        parser = LegislationCodeParser()
        parsed_codes = parser.parse_markdown(temp_md_path)
        
        # Step 4: Generate legislation comparison metrics
        legislation_path = Path("legislation_util/unique_sections_legislation.json")
        legislation_info = load_legislation_unique_sections(legislation_path)
        
        # Save parsed_codes to temp file for parse_submission_codes
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as temp_parsed:
            json.dump(parsed_codes, temp_parsed)
            temp_parsed_path = temp_parsed.name
        
        submission_raw_codes, submission_norm_codes = parse_submission_codes(Path(temp_parsed_path))
        metrics = compute_metrics(legislation_info, submission_raw_codes, submission_norm_codes)
        
        # Step 5: Generate issues using compare_chunks logic
        init_openai_from_env()
        
        # Load legislation
        legislation = load_legislation(Path("legislation_util/legislation.json"))
        
        all_main_codes_found = metrics.get("all_main_codes_found", [])
        all_issues = []
        
        for code in all_main_codes_found:
            if not isinstance(code, str):
                continue
            
            # Get legislation text
            leg_info = get_subsections_for_code(code, legislation)
            legislation_markdown = (leg_info.get("main_section") or "").strip()
            subsections_md = (leg_info.get("subsections_markdown") or "").strip()
            if subsections_md:
                if legislation_markdown:
                    legislation_markdown += "\n\n\n" + subsections_md
                else:
                    legislation_markdown = subsections_md
            
            # Get submission text
            submission_text = get_submission_by_codes([code])
            if not submission_text.strip():
                continue
            
            # Call OpenAI for issues
            result: IssueList = call_openai_for_issues(code, legislation_markdown, submission_text)
            
            # Find sections
            submission_sections = find_sections_for_code(code, parsed_codes)
            
            for issue in result.issues:
                issue.main_code = issue.main_code or code
                issue.submission_sections = submission_sections
                all_issues.append(issue.model_dump())
        
        issues_output = {"issues": all_issues}
        
        # Step 6: Clean up temp files
        for path in [temp_pdf_path, temp_md_path, temp_parsed_path]:
            if path and os.path.exists(path):
                os.unlink(path)
        
        # Return all outputs
        return ParseResponse(
            markdown=full_markdown,
            parsed_codes=parsed_codes,
            metrics=metrics,
            issues=issues_output
        )
    
    except Exception as e:
        # Clean up on error
        for path in [temp_pdf_path, temp_md_path, temp_parsed_path]:
            if path and os.path.exists(path):
                os.unlink(path)
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)