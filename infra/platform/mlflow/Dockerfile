FROM python:3.12-slim
# >=3.15 fixes CVE-2026-64849 (unauthenticated SSRF in webhook delivery)
RUN pip install --no-cache-dir "mlflow>=3.15,<4" psycopg2-binary boto3
COPY start.sh /start.sh
RUN chmod +x /start.sh
EXPOSE 5000
CMD ["/start.sh"]
