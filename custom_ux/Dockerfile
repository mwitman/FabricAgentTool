# -----------------------------------------------------------
# Stage 1: Build the React frontend
# -----------------------------------------------------------
FROM node:20-alpine AS frontend-build

WORKDIR /app/frontend
ARG VITE_ENTRA_CLIENT_ID
ARG VITE_ENTRA_TENANT_ID
ENV VITE_ENTRA_CLIENT_ID=$VITE_ENTRA_CLIENT_ID
ENV VITE_ENTRA_TENANT_ID=$VITE_ENTRA_TENANT_ID
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build
# Output lands in /app/frontend/../backend/static (= /app/backend/static)

# -----------------------------------------------------------
# Stage 2: Python runtime
# -----------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python deps
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/ ./backend/

# Copy the built frontend from stage 1
COPY --from=frontend-build /app/backend/static ./backend/static

# Expose port
EXPOSE 8080

# Run
CMD ["python", "-m", "uvicorn", "backend.server:app", "--host", "0.0.0.0", "--port", "8080"]
 