# Python API Template

A simple Python FastAPI template with Docker support and automated CI/CD pipeline using GitHub Actions.

## Features

- ✅ FastAPI web framework
- ✅ Single health check endpoint (`/healthz`)
- ✅ Docker containerization
- ✅ GitHub Actions CI/CD pipeline
- ✅ Multi-architecture Docker builds (amd64, arm64)
- ✅ Automated image tagging for develop branch and releases

## API Endpoints

### GET /healthz
Returns the health status of the API.

**Response:**
```json
{
  "status": "healthy",
  "message": "API is running successfully"
}
```

## Local Development

### Prerequisites
- Python 3.11+
- pip

### Setup
1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd python-api-template
   ```


2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the application:
   ```bash
   python main.py
   ```

4. The API will be available at `http://localhost:8000`
5. Visit `http://localhost:8000/docs` for interactive API documentation

## Docker

### Build the image
```bash
docker build -t python-api-template .
```

### Run the container
```bash
docker run -p 8000:8000 python-api-template
```

### Test the health endpoint
```bash
curl http://localhost:8000/healthz
```

## CI/CD Pipeline

The GitHub Actions workflow provides comprehensive CI/CD with three different scenarios:

### 🔄 Pull Request Testing
When a PR is created or updated targeting the `develop` branch:
1. **Builds** the Docker image (no push)
2. **Tests** the API health endpoint
3. **Comments** on PR with build status and test results

### 🚀 Develop Branch Deployment
When code is pushed to `develop` branch:
1. **Builds** the Docker image
2. **Pushes** to GitHub Container Registry tagged as `develop`

### 🏷️ Tag Releases
When a tag is created:
1. **Builds** the Docker image
2. **Pushes** to GitHub Container Registry with the tag name

### Image Naming Convention

- **Develop branch pushes**: `ghcr.io/owner/repo:develop`
- **Tag releases**: `ghcr.io/owner/repo:tag-name`
- **PR testing**: Images built but not pushed (local testing only)

### Workflow Triggers

The workflow runs on:
- **Pull requests** to `develop` branch (opened, synchronized, reopened)
- **Push** to `develop` branch
- **Creation** of any tag

### Required Permissions

The workflow uses `GITHUB_TOKEN` with the following permissions:
- `contents: read` - to checkout the repository
- `packages: write` - to push to GitHub Container Registry

## Project Structure

```
python-api-template/
├── .github/
│   └── workflows/
│       └── docker-build-push.yml    # CI/CD pipeline
├── main.py                          # FastAPI application
├── requirements.txt                 # Python dependencies
├── Dockerfile                       # Docker configuration
└── README.md                        # This file
```

## Security Features

- Non-root user in Docker container
- Health check endpoint for container monitoring
- Minimal base image (Python slim)
- No cache directories to reduce image size

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test locally
5. Submit a pull request

## License

This project is licensed under the MIT License.