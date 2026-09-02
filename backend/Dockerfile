FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
# Pin resolution to PyTorch's CPU wheel index before installing the remaining
# project dependencies; otherwise Linux may pull multi-GB CUDA runtime wheels.
RUN pip install --no-cache-dir "torch>=2.9,<3" \
      --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -e ".[dev]"

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
