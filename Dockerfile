# HumanKey API (run.py): POST /run on port 8000. No GPU needed; sample mode needs no services or keys.
# Base images from the AWS mirror of Docker Hub's official images (same images, no Docker Hub pull limit).
FROM public.ecr.aws/docker/library/node:22-slim AS node

FROM public.ecr.aws/docker/library/python:3.11-slim
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY meterless/package.json meterless/package-lock.json meterless/
RUN cd meterless && npm ci --no-audit --no-fund
COPY . .
RUN mkdir -p logs && cp -n .env.example .env
EXPOSE 8000
CMD ["python", "run.py"]
