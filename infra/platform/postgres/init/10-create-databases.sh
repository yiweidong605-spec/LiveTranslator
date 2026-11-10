#!/bin/bash
# Runs once, when the postgres volume is first created.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
  CREATE USER mlflow WITH PASSWORD '${MLFLOW_DB_PASSWORD}';
  CREATE DATABASE mlflow OWNER mlflow;
  CREATE USER airflow WITH PASSWORD '${AIRFLOW_DB_PASSWORD}';
  CREATE DATABASE airflow OWNER airflow;
EOSQL
echo "created databases: mlflow, airflow"
