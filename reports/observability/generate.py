"""Gera snapshots agregados sem perguntas, respostas, IDs ou credenciais; veja --help."""
import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).with_name("index.html")
ZONE = ZoneInfo("America/Sao_Paulo")
FINISHED = {"SUCCESS", "ERROR", "REJECTED", "BLOCKED"}
AGENTS = {
    "memoria": "Contexto e memória",
    "memoria_resumo": "Resumo de conversa",
    "guardrail": "Guardrail",
    "orquestrador": "Orquestrador",
    "faq": "FAQ",
    "estoque": "Estoque",
    "vendas": "Vendas",
    "clientes": "Clientes",
    "analista": "Analista",
    "juiz": "Juiz",
    "memoria_extracao": "Memória de longo prazo",
}
GRAPH_NODES = set(AGENTS) - {"memoria_resumo", "memoria_extracao"}


def field(record, name):
    camel = re.sub(r"_([a-z])", lambda match: match[1].upper(), name)
    return record.get(name, record.get(camel))


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value >= 0 else None
    return None


def instant(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    # Os registros antigos do console usam datas UTC sem offset.
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


def stats(values):
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return {"count": 0, "mean": None, "p50": None, "p95": None}
    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)
    return {"count": len(ordered), "mean": round(mean(ordered), 3),
            "p50": percentile(.5), "p95": percentile(.95)}


def period_bounds(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("O início deve ser anterior ou igual ao fim.")
    return (datetime.combine(first, time.min, ZONE).astimezone(timezone.utc),
            datetime.combine(last + timedelta(days=1), time.min, ZONE).astimezone(timezone.utc))


def build_report(executions, traces, *, start=None, end=None, source="preview",
                 confirmed_resolutions=None, generated_at=None):
    """Filtra por início da execução; custos e latências não são somados em duplicidade."""
    if (start is None) != (end is None):
        raise ValueError("Informe início e fim juntos.")
    lower, upper = period_bounds(start, end) if start else (None, None)
    selected = {}
    invalid_dates = duplicates = 0
    for execution in executions:
        when = instant(field(execution, "started_at"))
        if when is None:
            invalid_dates += 1
            continue
        if lower is not None and not lower <= when < upper:
            continue
        key = str(execution.get("_id", execution.get("id", "")))
        if not key:
            raise ValueError("Execução sem identificador; não é possível correlacionar as métricas.")
        if key in selected:
            duplicates += 1
            continue
        selected[key] = execution
    grouped = defaultdict(list)
    seen = set()
    for trace in traces:
        key = str(field(trace, "execution_id"))
        if key not in selected:
            continue
        trace_id = trace.get("_id", trace.get("id"))
        if trace_id is not None:
            if str(trace_id) in seen:
                duplicates += 1
                continue
            seen.add(str(trace_id))
        grouped[key].append(trace)
    counts = Counter()
    latency, handoffs = [], []
    agents = defaultdict(lambda: {"durations": [], "errors": 0, "count": 0})
    daily = defaultdict(lambda: {"requests": 0, "errors": 0, "latencies": []})
    known_cost = complete_cost = 0.
    known_available = False
    complete_count = finished_count = 0
    for key, execution in selected.items():
        status = field(execution, "status")
        status = status if status in FINISHED | {"IN_PROGRESS"} else "UNKNOWN"
        counts[status] += 1
        day = instant(field(execution, "started_at")).astimezone(ZONE).date().isoformat()
        daily[day]["requests"] += 1
        daily[day]["errors"] += status == "ERROR"
        if status in FINISHED:
            finished_count += 1
            duration = number(field(execution, "total_latency_ms"))
            if duration is not None:
                latency.append(duration)
                daily[day]["latencies"].append(duration)
            total = number(field(execution, "estimated_cost_usd"))
            partial = number(field(execution, "known_cost_usd"))
            # Sem trace de modelo, um zero legado não comprova ausência de custo.
            has_model = any(field(trace, "langsmith_run_id") for trace in grouped[key])
            complete = field(execution, "cost_complete") is True and total is not None and has_model
            if complete:
                known_available = True
                complete_count += 1
                complete_cost += total
                known_cost += total
            elif partial is not None and (partial > 0 or any(
                    number(field(trace, "estimated_cost_usd")) is not None for trace in grouped[key])):
                known_available = True
                known_cost += partial
        if status not in FINISHED:
            continue
        ordered = []
        for trace in grouped[key]:
            name = field(trace, "agent_name")
            safe_name = name if name in AGENTS else "outro"
            metric = agents[safe_name]
            metric["count"] += 1
            metric["errors"] += field(trace, "status") == "ERROR"
            duration = number(field(trace, "latency_ms"))
            if duration is not None:
                metric["durations"].append(duration)
            begin, finish = instant(field(trace, "started_at")), instant(field(trace, "completed_at"))
            if name in GRAPH_NODES and begin is not None and finish is not None and finish >= begin:
                ordered.append((begin, finish))
        # Resumo e extração são etapas aninhadas: não criam transições entre nós.
        ordered.sort()
        for previous, current in zip(ordered, ordered[1:]):
            if current[0] >= previous[1]:
                handoffs.append((current[0] - previous[1]).total_seconds() * 1000)
    full_cost = finished_count > 0 and complete_count == finished_count
    if confirmed_resolutions is not None:
        if (isinstance(confirmed_resolutions, bool) or not isinstance(confirmed_resolutions, int)
                or not 0 <= confirmed_resolutions <= counts["SUCCESS"]):
            raise ValueError("Resoluções confirmadas devem estar entre zero e as respostas aprovadas.")
    return {
        "schema_version": 1,
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "source": source,
        "period": {"start": start, "end": end, "timezone": "America/Sao_Paulo"},
        "counts": {"requests": len(selected), "finished": finished_count,
                   "success": counts["SUCCESS"], "errors": counts["ERROR"],
                   "blocked": counts["BLOCKED"], "rejected": counts["REJECTED"],
                   "in_progress": counts["IN_PROGRESS"], "unknown": counts["UNKNOWN"],
                   "error_rate": counts["ERROR"] / finished_count if finished_count else None},
        "latency": {"graph_ms": stats(latency), "handoff_ms": stats(handoffs),
                    "agents": [{"name": AGENTS.get(name, "Outras etapas"), "calls": metric["count"],
                                "errors": metric["errors"], **stats(metric["durations"])}
                               for name, metric in sorted(agents.items())]},
        "cost": {"known_usd": round(known_cost, 8) if known_available else None,
                 "complete_usd": round(complete_cost, 8) if full_cost else None,
                 "complete_executions": complete_count,
                 "coverage": complete_count / finished_count if finished_count else None,
                 "mean_request_usd": round(complete_cost / finished_count, 8) if full_cost else None},
        "resolution": {"confirmed": confirmed_resolutions,
                       "source": "manual" if confirmed_resolutions is not None else None,
                       "cost_usd": (round(complete_cost / confirmed_resolutions, 8)
                                    if full_cost and confirmed_resolutions else None)},
        "daily": [{"day": day, "requests": metric["requests"], "errors": metric["errors"],
                   "mean_ms": stats(metric["latencies"])["mean"]} for day, metric in sorted(daily.items())],
        "quality": {"invalid_dates": invalid_dates, "duplicates_ignored": duplicates},
    }


def render_html(report):
    # Só o resultado agregado da função acima é serializado. Escape impede fechar o script JSON.
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False).replace("&", "\\u0026")
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")
    template = TEMPLATE.read_text(encoding="utf-8")
    result, count = re.subn(
        r'(<script id="report-data" type="application/json">).*?(</script>)',
        lambda match: match[1] + payload + match[2], template, flags=re.S,
    )
    if count != 1:
        raise ValueError("Template do relatório inválido.")
    return result


def read_bounded(api, resource, maximum):
    result = []
    for document in api.list(resource):
        if len(result) >= maximum:
            raise ValueError("Limite de leitura atingido. Nenhum relatório parcial foi gerado; ajuste --max-records.")
        result.append(document)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Prévia sem métricas nem chamadas externas.")
    mode.add_argument("--from-api", action="store_true", help="Lê a API Java configurada em AI_API_URL/AI_API_TOKEN.")
    mode.add_argument("--from-langsmith", action="store_true", help="Lê métricas técnicas do projeto LANGSMITH_PROJECT.")
    mode.add_argument("--input", type=Path, help="Exportação privada JSON com listas executions e traces.")
    parser.add_argument("--start", help="Primeiro dia, AAAA-MM-DD, em São Paulo.")
    parser.add_argument("--end", help="Último dia inclusivo, AAAA-MM-DD, em São Paulo.")
    parser.add_argument("--confirmed-resolutions", type=int, help="Contagem declarada e verificada pelo responsável.")
    parser.add_argument("--max-records", type=int, default=10000, help="Teto por recurso; exceder aborta a geração.")
    parser.add_argument("--graph-name", default="LangGraph", help="Nome da trace raiz do grafo no LangSmith.")
    parser.add_argument("--publish", action="store_true", help="Autoriza salvar o snapshot agregado em reports/observability/.")
    parser.add_argument("--output", type=Path, help="Destino privado; padrão: .cache/reports/observability.html.")
    args = parser.parse_args(argv)
    try:
        if not args.preview and (not args.start or not args.end):
            raise ValueError("Dados reais exigem --start e --end.")
        if args.preview and (args.start or args.end or args.confirmed_resolutions is not None):
            raise ValueError("A prévia publicada não aceita métricas ou período real.")
        if args.max_records < 1:
            raise ValueError("--max-records deve ser positivo.")
        if args.start:
            period_bounds(args.start, args.end)
        output = (args.output or ROOT / ".cache/reports/observability.html").resolve()
        # Publicação de métricas agregadas exige escolha explícita; o padrão continua privado.
        if not args.preview and output.is_relative_to(ROOT):
            publishing = args.publish and output.is_relative_to(TEMPLATE.parent)
            if not publishing and not any(output.is_relative_to(ROOT / folder) for folder in (".cache", "tmp")):
                raise ValueError("Salve métricas reais em .cache/, tmp/ ou fora do repositório.")
        executions, traces = [], []
        telemetry = None
        if args.from_api:
            from data.ai_api import get_ai_api
            api = get_ai_api()
            executions = read_bounded(api, "ai-executions", args.max_records)
            traces = read_bounded(api, "agent-traces", args.max_records)
        elif args.from_langsmith:
            from .langsmith_source import load_langsmith
            executions, traces, telemetry = load_langsmith(args.start, args.end, args.max_records, args.graph_name)
        elif args.input:
            if args.input.stat().st_size > 25 * 1024 * 1024:
                raise ValueError("Exportação excede 25 MiB; use uma amostra menor.")
            data = json.loads(args.input.read_text(encoding="utf-8"))
            executions, traces = data["executions"], data["traces"]
            if not isinstance(executions, list) or not isinstance(traces, list):
                raise ValueError("A exportação deve conter listas executions e traces.")
            if len(executions) > args.max_records or len(traces) > args.max_records:
                raise ValueError("Exportação excede --max-records.")
        report = build_report(executions, traces, start=args.start, end=args.end,
                              source=("preview" if args.preview else "api" if args.from_api
                                      else "langsmith" if args.from_langsmith else "file"),
                              confirmed_resolutions=args.confirmed_resolutions)
        if telemetry is not None:
            report["telemetry"] = telemetry
            report["counts"]["blocked"] = None
            report["counts"]["rejected"] = None
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_html(report), encoding="utf-8")
        print(f"Relatório gerado: {output}")
        return 0
    except ValueError as error:
        # Mensagens de validação são nossas; erros externos não devem expor tokens/URLs/dados.
        if isinstance(error, json.JSONDecodeError):
            parser.exit(2, "Exportação JSON inválida.\n")
        if type(error) is ValueError:
            parser.exit(2, str(error) + "\n")
        parser.exit(2, "Não foi possível validar os dados do relatório.\n")
    except Exception:
        parser.exit(2, "Não foi possível gerar o relatório. Confira acesso à API e formato dos dados.\n")


if __name__ == "__main__":
    raise SystemExit(main())
