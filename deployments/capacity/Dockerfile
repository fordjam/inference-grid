FROM python:3.12-slim
WORKDIR /app
COPY cloud_server.py capacity.py ./
COPY web ./web
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "cloud_server.py"]
