FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e .

COPY .local_instructions/mcp_http_server.py /app/mcp_http_server.py

VOLUME /palace
EXPOSE 3001

CMD ["python", "/app/mcp_http_server.py", "--palace", "/palace", "--port", "3001"]
