"""Fórmulas financeiras, amostras incompletas e privacidade do relatório."""
import json
import re
from datetime import datetime, timezone

import pytest

from reports.observability.generate import build_report, main, read_bounded, render_html


def execution(key, *, cost=.02, complete=True, status="SUCCESS", when="2026-11-02T12:00:00Z"):
    return {"id": key, "startedAt": when, "status": status, "totalLatencyMs": 1000,
            "estimatedCostUsd": cost, "knownCostUsd": .01, "costComplete": complete,
            "input": "PRIVATE QUESTION", "finalResponse": "PRIVATE ANSWER", "userId": 123}


def trace(execution_id, *, agent="faq", start="2026-11-02T12:00:00Z",
          end="2026-11-02T12:00:01Z", duration=1000):
    return {"executionId": execution_id, "agentName": agent, "status": "OK",
            "startedAt": start, "completedAt": end, "latencyMs": duration,
            "langsmithRunId": "PRIVATE TRACE ID"}


def test_report_keeps_error_block_and_rejection_separate_and_costs_once():
    executions = [execution("a"), execution("b", cost=.03, status="ERROR"),
                  execution("c", cost=.01, status="BLOCKED"), execution("d", cost=.04, status="REJECTED"),
                  execution("e", status="IN_PROGRESS")]
    report = build_report(executions, [trace(key) for key in "abcde"],
                          start="2026-11-02", end="2026-11-02", confirmed_resolutions=1)
    assert report["counts"]["error_rate"] == .25
    assert report["counts"]["finished"] == 4
    assert report["cost"]["complete_usd"] == .1
    assert report["cost"]["mean_request_usd"] == .025
    assert report["resolution"]["cost_usd"] == .1
    assert report["resolution"]["source"] == "manual"


def test_incomplete_cost_has_no_automatic_projection_or_resolution_cost():
    report = build_report([execution("a"), execution("b", cost=None, complete=False)],
                          [trace("a"), trace("b")], confirmed_resolutions=1)
    assert report["cost"]["known_usd"] == .03
    assert report["cost"]["coverage"] == .5
    assert report["cost"]["mean_request_usd"] is None
    assert report["cost"]["complete_usd"] is None
    assert report["resolution"]["cost_usd"] is None


def test_empty_cost_or_legacy_zero_does_not_mean_free_usage():
    empty = build_report([], [])
    assert empty["counts"]["error_rate"] is None
    assert empty["cost"]["known_usd"] is None
    assert empty["latency"]["graph_ms"]["p95"] is None
    zero = build_report([execution("a", cost=0)], [])
    assert zero["cost"]["coverage"] == 0
    assert zero["cost"]["mean_request_usd"] is None
    missing = execution("b", cost=None, complete=False)
    missing["knownCostUsd"] = 0
    assert build_report([missing], [trace("b")])["cost"]["known_usd"] is None


def test_dates_use_sao_paulo_inclusive_end_and_utc_for_legacy_records():
    records = [execution("before", when="2026-11-02T02:59:59Z"),
               execution("start", when="2026-11-02T03:00:00Z"),
               execution("legacy", when="2026-11-03 02:59:59"),
               execution("after", when="2026-11-03T03:00:00Z")]
    report = build_report(records, [], start="2026-11-02", end="2026-11-02")
    assert report["counts"]["requests"] == 2
    assert [day["day"] for day in report["daily"]] == ["2026-11-02"]


def test_nested_steps_do_not_inflate_handoff_or_total_latency():
    traces = [trace("a", agent="memoria", end="2026-11-02T12:00:02Z", duration=2000),
              trace("a", agent="memoria_resumo", end="2026-11-02T12:00:01Z"),
              trace("a", agent="guardrail", start="2026-11-02T12:00:02.050Z",
                    end="2026-11-02T12:00:02.200Z", duration=150)]
    report = build_report([execution("a")], traces)
    assert report["latency"]["handoff_ms"] == {"count": 1, "mean": 50, "p50": 50, "p95": 50}
    assert report["latency"]["graph_ms"]["mean"] == 1000


def test_export_is_an_allowlist_without_content_identifiers_or_script_injection():
    report = build_report([execution("PRIVATE EXECUTION")],
                          [trace("PRIVATE EXECUTION", agent="</script><script>alert('SECRET')</script>")])
    output = render_html(report)
    payload = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', output, re.S)[1]
    for private in ("PRIVATE QUESTION", "PRIVATE ANSWER", "PRIVATE EXECUTION", "PRIVATE TRACE ID", "SECRET"):
        assert private not in output
    assert json.loads(payload)["latency"]["agents"][0]["name"] == "Outras etapas"
    assert "https://" not in output
    assert "@@FONTS@@" not in output and "@@LOGO@@" not in output


@pytest.mark.parametrize("confirmed", [-1, 2, True, .5])
def test_approvals_are_not_automatically_counted_as_confirmed_resolutions(confirmed):
    with pytest.raises(ValueError):
        build_report([execution("a")], [trace("a")], confirmed_resolutions=confirmed)
    assert build_report([execution("a")], [trace("a")])["resolution"]["confirmed"] is None


def test_pagination_cap_fails_instead_of_publishing_a_truncated_report():
    class FakeApi:
        def list(self, resource):
            yield execution("a")
            yield execution("b")
    with pytest.raises(ValueError, match="Nenhum relatório parcial"):
        read_bounded(FakeApi(), "ai-executions", 1)


def test_real_report_is_written_privately_and_preview_needs_no_credentials(tmp_path, monkeypatch):
    private_input = tmp_path / "input.json"
    private_input.write_text(json.dumps({"executions": [execution("a")], "traces": [trace("a")]}))
    import reports.observability.generate as generator
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    assert main(["--preview", "--output", str(tmp_path / "preview.html")]) == 0
    with pytest.raises(SystemExit):
        main(["--input", str(private_input), "--start", "2026-11-02", "--end", "2026-11-02",
              "--output", str(tmp_path / "public.html")])
    assert not (tmp_path / "public.html").exists()
    assert main(["--input", str(private_input), "--start", "2026-11-02", "--end", "2026-11-02"]) == 0
    generated = tmp_path / ".cache/reports/observability.html"
    assert generated.exists()
    assert "PRIVATE QUESTION" not in generated.read_text(encoding="utf-8")


def test_duplicates_are_ignored_and_nonfinite_metrics_are_not_serialized():
    record = execution("a", cost=float("nan"))
    report = build_report([record, record], [trace("a")],
                          generated_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert report["counts"]["requests"] == 1
    assert report["quality"]["duplicates_ignored"] == 1
    assert report["cost"]["complete_usd"] is None
    assert "NaN" not in render_html(report)


def test_real_api_client_exports_only_aggregates_using_get(tmp_path, monkeypatch):
    import httpx
    import data.ai_api as persistence
    import reports.observability.generate as generator

    key = "a" * 24
    calls = []
    def respond(request):
        calls.append((request.method, request.url.path))
        records = [execution(key)] if request.url.path.endswith("ai-executions") else [trace(key)]
        return httpx.Response(200, json={"items": records, "hasNext": False})

    client = persistence.AiApiClient("https://private.example.test", "PRIVATE TOKEN " * 4,
                                     transport=httpx.MockTransport(respond))
    monkeypatch.setattr(persistence, "get_ai_api", lambda: client)
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    try:
        assert main(["--from-api", "--start", "2026-11-02", "--end", "2026-11-02"]) == 0
    finally:
        client.http.close()
    output = (tmp_path / ".cache/reports/observability.html").read_text(encoding="utf-8")
    assert "PRIVATE TOKEN" not in output and "private.example.test" not in output
    assert '"mean_request_usd": 0.02' in output
    assert calls == [("GET", "/api/v1/ai-executions"), ("GET", "/api/v1/agent-traces")]


def test_langsmith_uses_trace_totals_excludes_standalone_runs_and_projects_fields():
    import asyncio
    from types import SimpleNamespace as NS
    from reports.observability.langsmith_source import SELECTS, read_langsmith
    begin = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    end = datetime(2026, 11, 2, 12, 0, 1, tzinfo=timezone.utc)
    def run(key, name, kind, cost, tokens, parents):
        return NS(id=key, trace_id="root", name=name, run_type=kind, total_cost=cost,
                  total_tokens=tokens, parent_run_ids=parents, start_time=begin,
                  end_time=end, status="success")
    root = run("root", "LangGraph", "chain", 0, None, [])
    standalone = run("other", "RunnableSequence", "chain", 999, 999, [])
    snapshots = [NS(root_run=root, trace_aggregates=NS(total_cost=.04)),
                 NS(root_run=standalone, trace_aggregates=NS(total_cost=999))]
    children = [root, run("node", "faq", "chain", .04, 300, ["root"]),
                run("llm1", "ChatOpenAI", "llm", .01, 100, ["root", "node"]),
                run("llm2", "ChatOpenAI", "llm", .03, 200, ["root", "node"])]
    calls = []
    class Query:
        def __init__(self, items):
            self.items = items
        def query(self, **params):
            calls.append(params)
            async def iterate():
                for item in self.items:
                    yield item
            return iterate()
    class Client:
        traces = Query(snapshots)
        runs = Query(children)
        async def aread_project(self, **params):
            return NS(id="PRIVATE PROJECT")
    executions, traces, telemetry = asyncio.run(read_langsmith(
        Client(), "PRIVATE PROJECT", "2026-11-02", "2026-11-02", 100))
    assert len(executions) == 1
    assert executions[0]["estimated_cost_usd"] == .04
    assert executions[0]["cost_complete"] is True
    assert len(traces) == 1 and traces[0]["agent_name"] == "faq"
    assert telemetry["known_tokens"] == 300 and telemetry["model_calls"] == 2
    assert all(call["selects"] == SELECTS for call in calls)
    assert not set(SELECTS) & {"INPUTS", "OUTPUTS", "ERROR", "EXTRA", "METADATA"}
    assert all("min_start_time" in call and "max_start_time" in call for call in calls)


def test_failed_model_without_usage_does_not_prove_zero_cost():
    from types import SimpleNamespace as NS
    from reports.observability.langsmith_source import latency_ms, usage_confirmed
    assert not usage_confirmed(NS(total_cost=0, total_tokens=None, status="error"))
    assert usage_confirmed(NS(total_cost=.01, total_tokens=20, status="error"))
    assert usage_confirmed(NS(total_cost=0, total_tokens=0, status="SUCCESS"))
    assert latency_ms(NS(latency_seconds=1.25)) == 1250


def test_publishing_requires_opt_in_and_does_not_relax_aggregate_privacy(tmp_path, monkeypatch):
    import reports.observability.generate as generator
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    monkeypatch.setattr(generator, "TEMPLATE", tmp_path / "reports/observability/index.html")
    generator.TEMPLATE.parent.mkdir(parents=True)
    generator.TEMPLATE.write_text('<script id="report-data" type="application/json">{}</script>')
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps({"executions": [execution("a")], "traces": [trace("a")]}))
    args = ["--input", str(raw), "--start", "2026-11-02", "--end", "2026-11-02",
            "--output", str(generator.TEMPLATE)]
    with pytest.raises(SystemExit):
        main(args)
    assert main(args + ["--publish"]) == 0
    output = generator.TEMPLATE.read_text(encoding="utf-8")
    assert "PRIVATE QUESTION" not in output and "PRIVATE TRACE ID" not in output
