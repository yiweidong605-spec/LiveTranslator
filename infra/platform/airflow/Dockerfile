FROM apache/airflow:3.4.0
# Pin apache-airflow to the base version so pip never upgrades it while adding our extras.
RUN pip install --no-cache-dir "apache-airflow==3.4.0" boto3 requests pyarrow
