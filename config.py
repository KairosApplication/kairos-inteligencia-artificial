import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

# O console do Windows costuma usar um code page legado (cp1252/cp850)
# que não cobre todos os caracteres Unicode que os modelos podem gerar
# (ex.: hífen não separável, aspas tipográficas). Sem isso, print()
# pode lançar UnicodeEncodeError e derrubar o chat no meio de uma
# resposta.
if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# CONFIGURAÇÃO
load_dotenv()


def _agora_dt() -> datetime:
    """
    Retorna o instante atual em UTC, utilizado para medir a latência
    de cada nó do grafo antes de registrá-la em `agent_traces`.
    """
    return datetime.now(timezone.utc)


# MEMÓRIA DE LONGO PRAZO
#
# Usuário padrão do chat de teste. Cada user_id possui sua própria
# conversa ativa e memória persistida no MongoDB (coleção
# `conversations` do banco `kairos_ai`).
USUARIO_PADRAO_ID = 1

# Limiares do SummarizationMiddleware. Intencionalmente baixos para
# que a sumarização seja fácil de observar durante os testes; em um
# ambiente real esses valores tendem a ser maiores (ex.: na faixa de
# várias dezenas de mensagens ou milhares de tokens).
RESUMO_GATILHO_MENSAGENS = 10
RESUMO_MANTER_MENSAGENS = 4
