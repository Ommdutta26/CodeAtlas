FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
ENV CODEATLAS_ROOTS=/repos CODEATLAS_HOME=/cache
CMD ["codeatlas-mcp"]
