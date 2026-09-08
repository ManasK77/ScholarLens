FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
# Install the CPU wheel first so dependency resolution does not pull CUDA into
# this local service image (the same requirement documented in pyproject.toml).
RUN pip install --no-cache-dir "torch>=2.9,<3" \
    --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -e ".[dev]"

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
