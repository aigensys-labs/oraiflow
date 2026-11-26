"""Regression coverage for psycopg IndeterminateDatatype in JSONB builders.

Set TEST_DATABASE_URL to a disposable PostgreSQL database to run this test.
The v0.2 failure involved untyped string bind parameters inside jsonb_build_object.
"""
import json
import os

import pytest


def test_jsonb_build_object_accepts_explicitly_cast_text_parameters():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL-specific SQL regression")
    sqlalchemy = pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg")
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    try:
        # Reproduce the old polymorphic bind failure on PostgreSQL 17/psycopg.
        with engine.connect() as conn:
            with pytest.raises(sqlalchemy.exc.ProgrammingError, match="IndeterminateDatatype"):
                conn.execute(text("""
                    SELECT jsonb_build_object(
                        'amount_minor', :amount,
                        'currency', :currency,
                        'beneficiary', :beneficiary
                    ) AS detail
                """), {"amount": 2500, "currency": "EUR", "beneficiary": "John Carter"}).scalar_one()
            conn.rollback()

        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT jsonb_build_object(
                    'amount_minor', :amount,
                    'currency', CAST(:currency AS text),
                    'beneficiary', CAST(:beneficiary AS text)
                ) AS detail
            """), {"amount": 2500, "currency": "EUR", "beneficiary": "John Carter"}).scalar_one()
            assert result == {"amount_minor": 2500, "currency": "EUR", "beneficiary": "John Carter"}

            # The MCP role migration must type the polymorphic format()
            # argument explicitly; %L then safely quotes apostrophes.
            from shared.database import _build_mcp_role_ddl
            create_sql = _build_mcp_role_ddl(conn, "reader's-local-password", False)
            assert "CREATE ROLE oraiflow_mcp_reader" in create_sql
            assert "PASSWORD 'reader''s-local-password'" in create_sql
            alter_sql = _build_mcp_role_ddl(conn, "reader's-local-password", True)
            assert "ALTER ROLE oraiflow_mcp_reader" in alter_sql
            assert "PASSWORD 'reader''s-local-password'" in alter_sql

            # This is the v0.3 audit pattern: serialize a typed Python object,
            # then pass one bind parameter explicitly cast to JSONB.
            evidence = {"currency": "EUR", "beneficiary": "John Carter", "sca": "demo-confirmed"}
            stored = conn.execute(text("SELECT CAST(:detail AS jsonb)"),
                                  {"detail": json.dumps(evidence)}).scalar_one()
            assert stored == evidence
    finally:
        engine.dispose()
