from config import USUARIO_PADRAO_ID
from data.observability import registrar_feedback
from graph import grafo
from schemas import GraphState


# FUNÇÃO DE TESTE
def testar_pergunta(pergunta: str, user_id: int = USUARIO_PADRAO_ID):

    """
    Executa uma única pergunta pelo grafo.

    O histórico de mensagens e o resumo da conversa do usuário são
    carregados do MongoDB no início da execução (node "memoria") e
    persistidos novamente ao final, caracterizando memória de longo
    prazo entre execuções distintas do chat de teste.
    """

    estado_inicial: GraphState = {
        "pergunta": pergunta,
        "resposta": "",
        "rota": "",
        "aprovado": False,
        "motivo_avaliacao": "",
        "tentativas": 0,
        "bloqueado": False,
        "categoria_bloqueio": "",
        "user_id": user_id,
        "conversation_id": None,
        "historico": [],
        "memory_context": None,
        "memorias_usuario": [],
        "execution_id": None
    }

    print("\n" + "=" * 60)
    print("KAIROS — TESTE DO GRAFO")
    print("=" * 60)

    print("\nPergunta:")
    print(pergunta)

    # Executa o fluxo completo.
    resultado = grafo.invoke(estado_inicial)

    print("\n" + "=" * 60)
    print("RESULTADO FINAL")
    print("=" * 60)

    print(f"\nGuardrail:")
    print("BLOQUEADO" if resultado["bloqueado"] else "LIBERADO")

    if resultado["bloqueado"]:
        print(f"Categoria: {resultado['categoria_bloqueio']}")

    print(f"\nRota escolhida:")
    print(resultado["rota"] or "(não roteado — mensagem bloqueada pelo guardrail)")

    print(f"\nTentativas:")
    print(resultado["tentativas"])

    print(f"\nJuiz:")
    if resultado["bloqueado"]:
        print("(não executado — mensagem bloqueada pelo guardrail)")
    else:
        print("APROVADO" if resultado["aprovado"] else "REPROVADO")
        print(f"\nMotivo da avaliação:")
        print(resultado["motivo_avaliacao"])

    print(f"\nResposta:")
    print(resultado["resposta"])

    print("\n" + "=" * 60)

    return resultado


def solicitar_feedback(resultado: dict) -> None:
    """
    Pergunta ao usuário, no chat de teste, se ele deseja avaliar a
    resposta recebida (nota de 1 a 5 e comentário opcional),
    persistindo a avaliação na collection `feedback`, vinculada à
    execução (`execution_id`) que gerou a resposta.

    Não solicita feedback quando a mensagem foi bloqueada pelo
    Guardrail, já que nesse caso não houve uma resposta de negócio
    para avaliar.
    """
    if resultado.get("bloqueado") or not resultado.get("execution_id"):
        return

    resposta_usuario = input(
        "\nDeseja avaliar essa resposta? (1-5, ou Enter para pular): "
    ).strip()

    if not resposta_usuario:
        return

    try:
        rating = int(resposta_usuario)
    except ValueError:
        print("[FEEDBACK] Nota inválida, feedback ignorado.")
        return

    if rating < 1 or rating > 5:
        print("[FEEDBACK] A nota deve estar entre 1 e 5. Feedback ignorado.")
        return

    comentario = input("Comentário (opcional, Enter para pular): ").strip() or None

    registrar_feedback(
        resultado["execution_id"], resultado["user_id"], rating, comentario
    )

    print("[FEEDBACK] Obrigado! Sua avaliação foi registrada.")


# EXECUÇÃO
COMANDOS_SAIDA = {"sair", "exit", "quit", "q"}

if __name__ == "__main__":

    print("\n" + "=" * 60)
    print("KAIROS — CHAT DE TESTE")
    print("=" * 60)
    print(
        "\nConverse com os agentes do Kairos. "
        "Digite 'sair' para encerrar."
    )
    print(
        "A conversa é persistida no MongoDB (memória de longo prazo). "
        f"Usuário de teste: {USUARIO_PADRAO_ID}."
    )

    while True:
        pergunta = input("\nVocê: ").strip()

        if not pergunta:
            continue

        if pergunta.lower() in COMANDOS_SAIDA:
            print("\nEncerrando o chat. Até a próxima!")
            break

        try:
            resultado = testar_pergunta(pergunta)
            solicitar_feedback(resultado)

        except KeyboardInterrupt:
            raise

        except Exception as erro:
            print(f"\n[ERRO] Ocorreu um problema ao processar a pergunta: {erro}")
