"""All DAGs import cleanly and keep the properties we rely on (CI job 'dags', needs Airflow)."""
import os
import tempfile

import pytest

pytestmark = pytest.mark.airflow
os.environ.setdefault("AIRFLOW_HOME", tempfile.mkdtemp(prefix="airflow-home-"))  # never touch ~/airflow
os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
airflow = pytest.importorskip("airflow")

from conftest import DAGS  # noqa: E402


@pytest.fixture(scope="module")
def bag():
    from airflow.models.dagbag import DagBag

    return DagBag(dag_folder=str(DAGS))


def test_no_import_errors(bag):
    assert bag.import_errors == {}


def test_expected_dags(bag):
    assert {"platform_healthcheck", "logbook_daily"} <= set(bag.dag_ids)


def test_helpers_are_not_parsed_as_dags(bag):
    assert not any("lt_lib" in (d.fileloc or "") for d in bag.dags.values())


def test_every_dag_has_tags_and_no_catchup(bag):
    for dag in bag.dags.values():
        assert dag.tags, f"{dag.dag_id} has no tags"
        assert dag.catchup is False, f"{dag.dag_id} would backfill"


def test_healthcheck_watches_the_backup(bag):
    tasks = set(bag.dags["platform_healthcheck"].task_ids)
    assert {"check_internet", "check_backup", "report"} <= tasks


def test_logbook_runs_one_at_a_time(bag):
    dag = bag.dags["logbook_daily"]
    assert dag.max_active_runs == 1  # runs share _state.json
    assert {"plan", "process", "finalize"} == set(dag.task_ids)
