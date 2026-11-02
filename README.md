# Kairos chatbot

Chatbot para consultar a documentação do Kairos, estoque, vendas e ofertas.
LangChain cria os agentes; LangGraph orquestra memória, guardrail, roteamento,
especialista e juiz. PostgreSQL fornece os dados operacionais e MongoDB persiste
conversas, memórias e rastreamento.

## Preparação

Use Python 3.12. No PowerShell, dentro do projeto:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Se já existe um .env, preserve-o e acrescente apenas as configurações necessárias.
Preencha OPENAI_API_KEY e GROQ_API_KEY, as credenciais PostgreSQL e MONGODB_URI.
LangSmith é opcional. Nunca versione o .env.

## Provedores e configuração

Os agentes de chat usam OpenAI como provedor principal, com gpt-6-luna
instanciado em llms.py. O FAQ gera embeddings multilíngues localmente com
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 via FastEmbed.

Quando uma chamada de chat OpenAI retorna RateLimitError (HTTP 429), somente
aquele passo passa para Groq. O SDK de chat não repete a chamada antes do fallback.
Erros de autenticação, entrada e rede continuam sendo erros, sem mascaramento.
Se Groq também falhar, o erro é propagado. Uma nova chamada tenta OpenAI novamente.

O FAQ mantém o índice FAISS e não exige uma API de embeddings ou Qdrant hospedado.
A primeira execução baixa os pesos do modelo público (aproximadamente 220 MB)
para .cache/faq-models; depois os embeddings são calculados em CPU.
Um cache SQLite ignorado pelo Git separa vetores por modelo e por tipo de texto
(consulta ou documento). O índice FAISS é montado na primeira consulta de cada
processo. Para uma implantação sem acesso à internet, prepare o cache do modelo
antes de iniciar o serviço.

Os parâmetros de chunking, MMR e execução do modelo local estão em config.py.
O notebook antigo usa E5: seu resultado de 29/30 perguntas não valida o modelo
atual. Reavalie a recuperação antes de anunciar métricas.

## Executar e validar

```powershell
.venv/Scripts/python.exe test.py
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe .github/scripts/validate.py
```

test.py é um console manual que usa serviços externos. Os testes em tests/
usam respostas simuladas e bloqueiam conexões de rede; não consomem APIs pagas.
As verificações de sintaxe não substituem testes de integração.

## Versionamento

Use branches feat/, fix/ e chore/ com escopo descritivo. Cada nova funcionalidade
começa em sua própria branch.
Use commits Conventional Commits e PRs para main, respeitando as revisões e
checks existentes. Não faça force-push nem reescreva o histórico compartilhado.
