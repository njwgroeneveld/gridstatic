FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY src/ ./src/
COPY log_config.json .
ENV PYTHONPATH=/app
# Exported as grid_build_info{commit}, so a dashboard can tell which build runs.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=$GIT_COMMIT
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-config", "log_config.json"]
