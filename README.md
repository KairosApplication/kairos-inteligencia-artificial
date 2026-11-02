# Kairos chatbot

Chatbot para consultar a documentação do Kairos, estoque, vendas e ofertas.
LangChain cria os agentes; LangGraph conduz memória → guardrail → roteamento →
especialista → juiz. O analista combina indicadores históricos do Databricks
com os especialistas de estoque e vendas.

## Estrutura

```text
app/                     API, agentes, grafo, autenticação e memória
  analyst/               Integração do analista por MCP e A2A
  integrations/          Clientes PostgreSQL, MongoDB, API Java e Databricks
  tools/                 Ferramentas dos especialistas
data/                    PDF e demais arquivos consumidos pela aplicação
observability/           Traces, métricas e integração LangSmith
  reports/               Gerador, HTML e recursos do relatório
  evaluations/           Notebook histórico de avaliação do RAG
tests/                   Testes offline
```

## Preparação

Use Python 3.12. No PowerShell, dentro do projeto:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Para implantação, instale `requirements.txt`. Preencha o `.env` local ou as
variáveis do ambiente de execução; nunca envie credenciais ao GitHub.
Preserve o `.env` existente e use [.env.example](.env.example) como referência.

## Configuração e integrações

| Uso | Configuração |
| --- | --- |
| Chat e fallback | `OPENAI_API_KEY` e `GROQ_API_KEY` |
| Dados operacionais e vínculos dos usuários | `POSTGRESQL_HOST`, `POSTGRESQL_PORT`, `POSTGRESQL_DB`, `POSTGRESQL_USER`, `POSTGRESQL_PASSWORD` |
| Persistência da FastAPI via Java `kairos-api-ia` | `PERSISTENCE_BACKEND=api`, `AI_API_URL` e `AI_API_TOKEN` correspondente ao backend Java |
| Login desktop via Spring | `SPRING_AUTH_URL` e `CHAT_ALLOWED_ORIGINS` com as origens reais do frontend; cookie de sessão configurável por `SPRING_SESSION_COOKIE_NAME` |
| Login mobile via Firebase | `FIREBASE_PROJECT_ID` e credenciais Admin no servidor, por exemplo em `GOOGLE_APPLICATION_CREDENTIALS` |
| Delegação ao analista | `ANALYST_A2A_TOKEN`: segredo do backend com pelo menos 32 caracteres |
| Rotas internas entre serviços | `CHATBOT_SERVICE_TOKEN`: segredo do backend com pelo menos 32 caracteres |
| Observabilidade opcional | `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` e `LANGSMITH_PROJECT` |

No modo `api`, a conexão MongoDB pertence à implantação Java. O console também
aceita `PERSISTENCE_BACKEND=mongodb`, com `MONGODB_URI` e `MONGODB_DATABASE` no
Python. Esse modo direto não habilita as rotas autenticadas da FastAPI.

### Sessões e acesso

O desktop reutiliza os cookies de login e o cabeçalho `X-CSRF-TOKEN`; o Spring
valida a sessão em `/api/v1/auth/chat-context`. O mobile envia o ID token Firebase
em `Authorization: Bearer ...`. Perfil, empresa e vínculos são consultados no
PostgreSQL, sem aceitar esses valores da mensagem do usuário.

| Perfil | Domínios permitidos |
| --- | --- |
| Gerente | FAQ, estoque, vendas, clientes e analista da sua empresa |
| Repositor | FAQ e estoque dos setores atribuídos |
| Cliente | FAQ e seus próprios dados de compras/ofertas |

Conversas e memórias consideram usuário, perfil, empresa, cliente e setores.
O `SummarizationMiddleware` resume turnos aprovados e persiste os marcadores com
controle de concorrência. Cada resposta expõe apenas as fontes efetivamente
consultadas (`pdf`, `database`, `memory`), sem nomes de tabelas ou coleções.

### Analista: Databricks, MCP e A2A

Configure host do workspace, cliente/segredo OAuth, warehouse e referências Gold
pelas variáveis listadas no `.env.example`. As consultas usam referências
permitidas, parâmetros de período/empresa e limites; o modelo não envia SQL livre.

O MCP privado inicia e encerra no ciclo de vida da FastAPI. A delegação A2A usa
`/a2a/analyst` na mesma aplicação, com identidade assinada de curta duração.
Não há outro comando, processo ou porta. `ANALYST_A2A_URL` só é necessário se o
analista for implantado em um serviço externo.

Os indicadores exigem a coluna configurada em `DATABRICKS_COMPANY_COLUMN`
(padrão `company_id`). `DATABRICKS_COMPANY_ID` só serve para um conjunto inteiro
comprovadamente exclusivo de uma empresa. O Gold atual ainda não preserva essa
separação; o acesso permanece bloqueado quando não é possível garantir o filtro.

A tool de dashboard consulta apenas metadados e requer o escopo OAuth
`dashboards` e vínculo exclusivo de empresa. Interpretação visual de gráficos e
execução de notebooks/pipelines ainda não estão implementadas.

## Modelos e FAQ

Os modelos ficam explícitos em [app/llms.py](app/llms.py): `gpt-6-luna`, com temperatura
0,6 no chat e 0 no modelo rápido. Um HTTP 429 da OpenAI encaminha somente o passo
afetado ao Groq (`openai/gpt-oss-120b` no chat e `openai/gpt-oss-20b` no rápido).
Outros erros são propagados; a próxima chamada tenta OpenAI novamente.

O FAQ usa OpenAI `text-embedding-3-small` (1536 dimensões) e recuperação FAISS/MMR.
O cache SQLite em `.cache/faq-embeddings.sqlite3` reutiliza vetores de documentos
e perguntas entre reinícios, separados por modelo e tipo de texto. Somente textos
ausentes são enviados à API, em lotes e sem retentativas automáticas. Preserve
esse cache na implantação para evitar novas cobranças de indexação; ele não é
versionado. O índice FAISS é reconstruído em memória a partir dos vetores salvos.

Chunking, recuperação e tamanho dos lotes estão em [app/config.py](app/config.py).
O resultado 29/30 do notebook antigo foi obtido com E5 e não valida o modelo atual.

## Executar e testar

```powershell
.venv/Scripts/python.exe -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```

`GET /health` verifica se a aplicação responde. Para o chat autenticado, use
`GET /api/v1/me/chat` para consultar os acessos e
`POST /api/v1/me/chat/messages` com `{"question":"Olá"}`. A resposta inclui
`conversationId`; envie esse campo nos próximos turnos. As rotas
`/api/v1/me/chat/conversations` permitem criar, listar e consultar conversas.
As rotas `/api/v1/chat/...` são internas e exigem o token de serviço.

```powershell
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe .github/scripts/validate.py
.venv/Scripts/python.exe -m app.cli
```

Os testes em `tests/` bloqueiam rede e não consomem modelos pagos. `app.cli` é um
console manual com serviços reais; não representa um login autenticado. A
validação ponta a ponta depende dos backends, credenciais e contas reais.

## Observabilidade

O [relatório HTML](observability/reports/index.html) reúne latência, erros,
custos, resolução e ROI projetado. Para gerar uma nova amostra do LangSmith:

```powershell
.venv/Scripts/python.exe -m observability.reports.generate --from-langsmith --start AAAA-MM-DD --end AAAA-MM-DD
```

O arquivo é salvo em `.cache/reports/observability.html`. Use `--preview` para
uma prévia sem rede; `--publish --output observability/reports/index.html`
publica somente agregados, sem perguntas, respostas, IDs ou credenciais.
Custos parciais e premissas de ROI são identificados no relatório.

## Versionamento

Use branches `feat/`, `fix/` e `chore/`, commits Conventional Commits e PRs para
`main`. Cada nova funcionalidade começa em sua própria branch. Preserve as
revisões, checks e o histórico compartilhado.
