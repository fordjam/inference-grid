"""One database per test when the suite runs against Postgres.

Under sqlite every fixture builds its ledger in `tmp_path`, so tests never see each other.
Under `GRID_TEST_DATABASE_URL` (CI's second pass) they all shared one database, and every
test that reuses a task id (`t1`, `copy-ok`), an account (`zai`) or a lane record collided
with the tests before it: 12 failures and 44 errors that never reproduced locally. Each test
now gets a fresh schema on the same server, named into `search_path` through the URL, and
the schema is dropped afterwards.
"""

import os
import uuid

import pytest


def _schema_url(base_url, schema):
    from sqlalchemy.engine import make_url

    scoped = make_url(base_url).update_query_dict({"options": f"-csearch_path={schema}"})
    return scoped.render_as_string(hide_password=False)


class _Schemas:
    """Fresh Postgres schemas on the test server, dropped when the test ends."""

    def __init__(self, base_url):
        from sqlalchemy import create_engine

        self.base_url = base_url
        self.admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
        self.names = []

    def fresh(self):
        from sqlalchemy import text

        schema = "t_" + uuid.uuid4().hex[:12]
        with self.admin.connect() as con:
            con.execute(text(f'CREATE SCHEMA "{schema}"'))
        self.names.append(schema)
        return _schema_url(self.base_url, schema)

    def close(self):
        from sqlalchemy import text

        with self.admin.connect() as con:
            for schema in self.names:
                con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        self.admin.dispose()


@pytest.fixture(autouse=True)
def _isolated_test_database(monkeypatch, request):
    url = os.environ.get("GRID_TEST_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        request.node._grid_schemas = None
        yield
        return
    schemas = _Schemas(url)
    request.node._grid_schemas = schemas
    monkeypatch.setenv("GRID_TEST_DATABASE_URL", schemas.fresh())
    try:
        yield
    finally:
        schemas.close()


@pytest.fixture
def second_database(monkeypatch, request):
    """Point GRID_TEST_DATABASE_URL at another fresh database for the rest of the test —
    for a test that builds two worlds that must not see each other. A no-op under sqlite,
    where each world already has its own file."""

    def switch():
        schemas = getattr(request.node, "_grid_schemas", None)
        if schemas is not None:
            monkeypatch.setenv("GRID_TEST_DATABASE_URL", schemas.fresh())

    return switch
