from __future__ import annotations

import importlib
import sys
import types
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


def _load_mcp_module(monkeypatch):
    class _SessionManager:
        @asynccontextmanager
        async def run(self):
            yield

    class _FastMCP:
        def __init__(self, *args, **kwargs):
            self.session_manager = _SessionManager()
        def tool(self):
            return lambda function: function
        def streamable_http_app(self):
            from fastapi import FastAPI
            return FastAPI()

    package = types.ModuleType("mcp")
    package.__path__ = []
    server = types.ModuleType("mcp.server")
    server.__path__ = []
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _FastMCP
    transport = types.ModuleType("mcp.server.transport_security")
    transport.TransportSecuritySettings = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "mcp", package)
    monkeypatch.setitem(sys.modules, "mcp.server", server)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)
    monkeypatch.setitem(sys.modules, "mcp.server.transport_security", transport)
    sys.modules.pop("services.mcp.main", None)
    return importlib.import_module("services.mcp.main")


class _Result:
    def __init__(self, rows):
        self._rows = rows
    def mappings(self):
        return self
    def all(self):
        return self._rows


class _Connection:
    def __init__(self, summary_rows, group_rows, detail_rows):
        self.summary_rows = summary_rows
        self.group_rows = group_rows
        self.detail_rows = detail_rows
        self.calls = []
    def execute(self, statement, params):
        sql = str(statement)
        self.calls.append((sql, dict(params)))
        if "GROUP BY t.currency" in sql:
            return _Result(self.summary_rows)
        if "AS active_months" in sql:
            return _Result(self.group_rows)
        if "SELECT t.transaction_id" in sql:
            return _Result(self.detail_rows)
        raise AssertionError(f"Unexpected SQL: {sql}")


class _Engine:
    def __init__(self, connection):
        self.connection = connection
    def connect(self):
        class _Context:
            def __enter__(_self):
                return self.connection
            def __exit__(_self, *args):
                return False
        return _Context()


def test_calendar_periods_use_business_timezone_and_inclusive_customer_dates(monkeypatch):
    module = _load_mcp_module(monkeypatch)
    now = datetime(2026, 11, 24, 12, 0, tzinfo=ZoneInfo("Europe/Prague"))
    start, end, start_date, end_date, tz = module._transaction_analysis_period_bounds(
        "this_month", now=now, timezone_name="Europe/Prague",
    )
    assert start.isoformat().startswith("2026-11-01T00:00:00")
    assert end.isoformat().startswith("2026-11-25T00:00:00")
    assert (start_date, end_date, tz) == ("2026-11-01", "2026-11-24", "Europe/Prague")

    start, end, start_date, end_date, _ = module._transaction_analysis_period_bounds(
        "last_month", now=now, timezone_name="Europe/Prague",
    )
    assert start_date == "2026-10-01"
    assert end_date == "2026-10-31"
    assert end.isoformat().startswith("2026-11-01T00:00:00")


def test_coffee_analysis_uses_authenticated_filters_and_per_currency_aggregation(monkeypatch):
    module = _load_mcp_module(monkeypatch)
    connection = _Connection(
        summary_rows=[{"currency": "EUR", "transaction_count": 2, "sum_minor": 970, "average_minor": 485}],
        group_rows=[{"group_value": "Blue Bottle Coffee", "currency": "EUR", "transaction_count": 2,
                     "sum_minor": 970, "average_minor": 485, "active_months": 1}],
        detail_rows=[],
    )
    monkeypatch.setattr(module, "get_mcp_engine", lambda: _Engine(connection))
    monkeypatch.setattr(module, "_require_mcp_capability", lambda _cap: SimpleNamespace(subject="cust-a", tenant_id="tenant-a"))
    result = module.analyze_customer_transactions(
        period="this_month", flow="outflows", merchant_query="coffee", group_by="merchant",
    )
    import json
    envelope = json.loads(result)
    assert envelope["outcome"] == "SUCCESS"
    data = envelope["data"]
    assert data["transaction_count"] == 2
    assert data["summary_by_currency"] == [{"currency": "EUR", "transaction_count": 2, "sum_minor": 970, "average_minor": 485}]
    assert data["groups"][0]["group_value"] == "Blue Bottle Coffee"
    assert connection.calls[0][1]["owner"] == "cust-a"
    assert connection.calls[0][1]["tenant"] == "tenant-a"
    assert connection.calls[0][1]["merchant_query"] == "coffee"
    assert data["merchant_query"] == "coffee"
    assert "a.owner_id = :owner" in connection.calls[0][0]
    assert "a.tenant_id = :tenant" in connection.calls[0][0]
    assert "t.amount_minor < 0" in connection.calls[0][0]
    assert "UPPER(COALESCE(t.category, '')) <> 'TRANSFER'" in connection.calls[0][0]


def test_received_money_analysis_uses_positive_amount_predicate_and_period(monkeypatch):
    module = _load_mcp_module(monkeypatch)
    connection = _Connection(
        summary_rows=[{"currency": "EUR", "transaction_count": 4, "sum_minor": 120000, "average_minor": 30000}],
        group_rows=[], detail_rows=[],
    )
    monkeypatch.setattr(module, "get_mcp_engine", lambda: _Engine(connection))
    monkeypatch.setattr(module, "_require_mcp_capability", lambda _cap: SimpleNamespace(subject="cust-a", tenant_id="tenant-a"))
    import json
    envelope = json.loads(module.analyze_customer_transactions(period="last_30_days", flow="inflows", measure="sum"))
    assert envelope["outcome"] == "SUCCESS"
    assert envelope["data"]["flow"] == "inflows"
    assert envelope["data"]["period"] == "last_30_days"
    assert "t.amount_minor > 0" in connection.calls[0][0]
    assert "t.amount_minor < 0" not in connection.calls[0][0]


def test_salary_screen_defaults_to_inflows_and_longer_pattern_window(monkeypatch):
    module = _load_mcp_module(monkeypatch)
    connection = _Connection(
        summary_rows=[{"currency": "EUR", "transaction_count": 3, "sum_minor": 3300000, "average_minor": 1100000}],
        group_rows=[{"group_value": "Salary · Northstar Labs", "currency": "EUR", "transaction_count": 3,
                     "sum_minor": 3300000, "average_minor": 1100000, "active_months": 3}],
        detail_rows=[],
    )
    monkeypatch.setattr(module, "get_mcp_engine", lambda: _Engine(connection))
    monkeypatch.setattr(module, "_require_mcp_capability", lambda _cap: SimpleNamespace(subject="cust-a", tenant_id="tenant-a"))
    import json
    envelope = json.loads(module.analyze_customer_transactions(analysis_mode="recurring_inflows"))
    data = envelope["data"]
    assert data["period"] == "last_365_days"
    assert data["flow"] == "inflows"
    assert data["group_by"] == "merchant"
    assert data["groups"][0]["potentially_recurring"] is True
    assert "t.amount_minor > 0" in connection.calls[0][0]


def test_recurring_inflow_screen_does_not_accept_an_outflow_query(monkeypatch):
    module = _load_mcp_module(monkeypatch)
    monkeypatch.setattr(module, "_require_mcp_capability", lambda _cap: SimpleNamespace(subject="cust-a", tenant_id="tenant-a"))
    import json
    envelope = json.loads(module.analyze_customer_transactions(analysis_mode="recurring_inflows", flow="outflows"))
    assert envelope["outcome"] == "REJECTED"
    assert envelope["error"]["code"] == "INCOMPATIBLE_ANALYSIS_FILTERS"


def test_this_month_ends_after_current_local_day_not_at_next_month(monkeypatch):
    module = _load_mcp_module(monkeypatch)
    now = datetime(2026, 11, 24, 12, 0, tzinfo=ZoneInfo("Europe/Prague"))
    _start, end, start_date, end_date, _tz = module._transaction_analysis_period_bounds(
        "this_month", now=now, timezone_name="Europe/Prague",
    )
    assert start_date == "2026-11-01"
    assert end_date == "2026-11-24"
    assert end.isoformat().startswith("2026-11-25T00:00:00")


def test_aggregates_keep_currencies_separate_without_implicit_conversion(monkeypatch):
    module = _load_mcp_module(monkeypatch)
    connection = _Connection(
        summary_rows=[
            {"currency": "EUR", "transaction_count": 2, "sum_minor": 15000, "average_minor": 7500},
            {"currency": "USD", "transaction_count": 1, "sum_minor": 4200, "average_minor": 4200},
        ],
        group_rows=[], detail_rows=[],
    )
    monkeypatch.setattr(module, "get_mcp_engine", lambda: _Engine(connection))
    monkeypatch.setattr(module, "_require_mcp_capability", lambda _cap: SimpleNamespace(subject="cust-a", tenant_id="tenant-a"))
    import json
    envelope = json.loads(module.analyze_customer_transactions(period="this_month", flow="all", measure="sum"))
    data = envelope["data"]
    assert envelope["outcome"] == "SUCCESS"
    assert data["transaction_count"] == 3
    assert data["summary_by_currency"] == [
        {"currency": "EUR", "transaction_count": 2, "sum_minor": 15000, "average_minor": 7500},
        {"currency": "USD", "transaction_count": 1, "sum_minor": 4200, "average_minor": 4200},
    ]
    assert "total_minor" not in data


def test_zero_matching_transactions_returns_successful_empty_screen(monkeypatch):
    module = _load_mcp_module(monkeypatch)
    connection = _Connection(summary_rows=[], group_rows=[], detail_rows=[])
    monkeypatch.setattr(module, "get_mcp_engine", lambda: _Engine(connection))
    monkeypatch.setattr(module, "_require_mcp_capability", lambda _cap: SimpleNamespace(subject="cust-a", tenant_id="tenant-a"))
    import json
    envelope = json.loads(module.analyze_customer_transactions(period="this_month", merchant_query="not-present"))
    assert envelope["outcome"] == "SUCCESS"
    assert envelope["data"]["transaction_count"] == 0
    assert envelope["data"]["summary_by_currency"] == []


def test_excessive_group_results_are_marked_partial(monkeypatch):
    module = _load_mcp_module(monkeypatch)
    groups = [
        {"group_value": f"Merchant {index:03d}", "currency": "EUR", "transaction_count": 1,
         "sum_minor": 100, "average_minor": 100, "active_months": 1}
        for index in range(101)
    ]
    connection = _Connection(
        summary_rows=[{"currency": "EUR", "transaction_count": 101, "sum_minor": 10100, "average_minor": 100}],
        group_rows=groups, detail_rows=[],
    )
    monkeypatch.setattr(module, "get_mcp_engine", lambda: _Engine(connection))
    monkeypatch.setattr(module, "_require_mcp_capability", lambda _cap: SimpleNamespace(subject="cust-a", tenant_id="tenant-a"))
    import json
    envelope = json.loads(module.analyze_customer_transactions(group_by="merchant"))
    assert envelope["outcome"] == "PARTIAL"
    assert envelope["evidence"]["completeness"] == "PARTIAL"
    assert envelope["data"]["truncated"] is True
    assert len(envelope["data"]["groups"]) == 100


def test_monthly_coffee_analysis_uses_90_day_window_and_chronological_groups(monkeypatch):
    module = _load_mcp_module(monkeypatch)
    connection = _Connection(
        summary_rows=[{"currency": "EUR", "transaction_count": 3, "sum_minor": 1470, "average_minor": 490}],
        group_rows=[
            {"group_value": "2026-09", "currency": "EUR", "transaction_count": 1,
             "sum_minor": 465, "average_minor": 465, "active_months": 1},
            {"group_value": "2026-10", "currency": "EUR", "transaction_count": 1,
             "sum_minor": 485, "average_minor": 485, "active_months": 1},
            {"group_value": "2026-11", "currency": "EUR", "transaction_count": 1,
             "sum_minor": 520, "average_minor": 520, "active_months": 1},
        ],
        detail_rows=[],
    )
    monkeypatch.setattr(module, "get_mcp_engine", lambda: _Engine(connection))
    monkeypatch.setattr(module, "_require_mcp_capability", lambda _cap: SimpleNamespace(subject="cust-a", tenant_id="tenant-a"))

    import json
    envelope = json.loads(module.analyze_customer_transactions(
        period="last_90_days", flow="outflows", merchant_query="coffee", group_by="month",
    ))
    assert envelope["outcome"] == "SUCCESS"
    data = envelope["data"]
    assert data["period"] == "last_90_days"
    assert data["flow"] == "outflows"
    assert data["merchant_query"] == "coffee"
    assert data["group_by"] == "month"
    assert [row["group_value"] for row in data["groups"]] == ["2026-09", "2026-10", "2026-11"]
    group_sql, params = next(call for call in connection.calls if "AS active_months" in call[0])
    assert "DATE_TRUNC('month'" in group_sql
    assert "ORDER BY group_value ASC" in group_sql
    assert params["merchant_query"] == "coffee"
    assert "t.amount_minor < 0" in group_sql


def test_category_filter_is_returned_with_validated_analysis_result(monkeypatch):
    module = _load_mcp_module(monkeypatch)
    connection = _Connection(
        summary_rows=[{"currency": "EUR", "transaction_count": 1, "sum_minor": 3200, "average_minor": 3200}],
        group_rows=[], detail_rows=[],
    )
    monkeypatch.setattr(module, "get_mcp_engine", lambda: _Engine(connection))
    monkeypatch.setattr(module, "_require_mcp_capability", lambda _cap: SimpleNamespace(subject="cust-a", tenant_id="tenant-a"))

    import json
    envelope = json.loads(module.analyze_customer_transactions(
        period="last_30_days", flow="outflows", category_query="transport",
    ))
    assert envelope["outcome"] == "SUCCESS"
    assert envelope["data"]["category_query"] == "transport"
    assert connection.calls[0][1]["category_query"] == "transport"
    assert "POSITION(LOWER(:category_query) IN LOWER(COALESCE(t.category, ''))) > 0" in connection.calls[0][0]
