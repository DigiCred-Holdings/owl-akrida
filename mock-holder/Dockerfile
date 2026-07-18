FROM python:3.12-slim
RUN pip install --no-cache-dir 'aiohttp==3.11.18'
WORKDIR /app
COPY server.py .
EXPOSE 8090
CMD ["python", "-u", "server.py"]
