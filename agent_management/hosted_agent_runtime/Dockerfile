FROM python:3.12-slim
WORKDIR /app
ENV PORT=8088 PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
EXPOSE 8088
CMD ["python", "app.py"]
