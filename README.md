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

Todos os agentes usam OpenAI como provedor principal. Os padrões são
gpt-4.1-mini para chat e text-embedding-3-small para embeddings. Altere
OPENAI_CHAT_MODEL e OPENAI_FAST_MODEL para modelos compatíveis com ferramentas,
saída JSON Schema e temperatura. Os padrões foram escolhidos para uma primeira
integração econômica; avalie qualidade e acesso na sua conta antes da entrega.

Quando uma chamada de chat OpenAI retorna RateLimitError (HTTP 429), somente
aquele passo passa para Groq. O SDK de chat não repete a chamada antes do fallback.
Erros de autenticação, entrada e rede continuam sendo erros, sem mascaramento.
Se Groq também falhar, o erro é propagado. Uma nova chamada tenta OpenAI novamente.

O FAQ usa somente embeddings OpenAI: vetores de provedores diferentes não são
intercambiáveis. Um cache SQLite ignorado pelo Git reutiliza vetores por texto
e modelo entre reinicializações. A primeira consulta ao FAQ pode chamar a API;
consultas novas também precisam de embeddings e podem atingir limites.
O índice FAISS é montado na primeira consulta de cada processo.

Os parâmetros de chunking e MMR estão em .env.example. O notebook antigo usa E5:
seu resultado de 29/30 perguntas não valida os novos embeddings. Reavalie a
recuperação e as respostas com OpenAI antes de anunciar métricas.

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
