FROM python:3.14-alpine
WORKDIR /app
RUN adduser -D -u 1000 app && mkdir /data && chown app /data
COPY app.py ./
COPY static ./static
USER app
EXPOSE 8000
CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "8000", "--data", "/app/data.json", "--db", "/data/results.db"]
