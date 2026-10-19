from unittest.mock import Mock

import pytest
from bson import ObjectId
from langchain_core.messages import AIMessage, ToolMessage

import graph_nodes as nodes
from data.conversation_middleware import MemoryContext
from schemas import AvaliacaoJuiz


@pytest.fixture
def state(monkeypatch):
    for name in ["registrar_trace_agente", "registrar_avaliacao_juiz", "finalizar_execucao",
                 "registrar_turno", "_extrair_e_registrar_memorias"]:
        monkeypatch.setattr(nodes, name, Mock())
    conversation_id = ObjectId()
    return {"pergunta": "Qual o estoque?", "resposta": "Há 999 unidades.",
            "execution_id": ObjectId(), "conversation_id": conversation_id, "user_id": 1,
            "memory_context": MemoryContext(conversation_id), "rota": "estoque",
            "historico": [], "memorias_usuario": [], "tentativas": 2,
            "evidencias": [{"ferramenta": "estoque", "conteudo": "Há 12 unidades.", "status": "success"}]}


def test_reprovacao_final_nao_entrega_resposta_inventada(monkeypatch, state):
    monkeypatch.setattr(nodes, "juiz", Mock(invoke=Mock(return_value={
        "parsed": AvaliacaoJuiz(aprovado=False, motivo="Quantidade inventada")
    })))
    result = nodes.juiz_node(state)
    assert result["aprovado"] is False
    assert "999" not in result["resposta"]
    assert "confirmar" in result["resposta"]
    nodes.registrar_turno.assert_not_called()
    assert nodes.finalizar_execucao.call_args.args[1:] == ("REJECTED", result["resposta"])
    assert nodes.decidir_avaliacao({**state, **result}) == "fim"


def test_juiz_recebe_evidencias_e_persiste_so_aprovada(monkeypatch, state):
    state["resposta"] = "Há 12 unidades."
    judge = Mock(invoke=Mock(return_value={"parsed": AvaliacaoJuiz(aprovado=True, motivo="Dados conferem")}))
    monkeypatch.setattr(nodes, "juiz", judge)
    result = nodes.juiz_node(state)
    prompt = judge.invoke.call_args.args[0][-1].content
    assert '"ferramenta": "estoque"' in prompt
    assert "Há 12 unidades." in prompt
    assert result["aprovado"] is True
    nodes.registrar_turno.assert_called_once_with(state["conversation_id"], state["pergunta"], state["resposta"])


def test_nova_tentativa_recebe_feedback_e_evidencias_do_turno(state):
    state["motivo_avaliacao"] = "Corrigir quantidade"
    state["tentativas"] = 1
    state["historico"] = [ToolMessage(content="DADO ANTIGO", name="antiga", tool_call_id="old")]
    agent = Mock()
    agent.invoke.side_effect = lambda payload, **kwargs: {
        "messages": [*payload["messages"],
                     ToolMessage(content="Quantidade: 12", name="consulta", tool_call_id="new"),
                     AIMessage(content="Há 12 unidades.")]}
    result = nodes._executar_especialista("estoque", agent, state)
    inputs = agent.invoke.call_args.args[0]["messages"]
    assert any("Corrigir quantidade" in m.content for m in inputs)
    assert result["tentativas"] == 2
    assert result["evidencias"] == [{"ferramenta": "consulta", "conteudo": "Quantidade: 12", "status": "success"}]
