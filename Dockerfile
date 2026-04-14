FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements first for Docker layer caching
COPY GlucoRisk_Package/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY GlucoRisk_Package/ .

# Expose port
EXPOSE 5001

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5001/')" || exit 1

# Production entry point with Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "2", "--threads", "4", "--timeout", "120", "web_app:app"]
