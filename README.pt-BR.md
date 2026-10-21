# Sistema Multiagente de Atendimento Getnet

**[Read in English](README.md)**

Um assistente multiagente de atendimento para lojistas da Getnet. Um **Router Agent** lê cada mensagem (junto com a
conversa até ali) e devolve a rota e um **plano**: um especialista, ou dois quando a mensagem mistura a conta do próprio
lojista com uma regra geral. Os especialistas são o **Knowledge Agent** (RAG sobre os sites da Getnet, com busca medida, + busca na
web) e o **Customer Support Agent** (ferramentas sobre os dados da conta do lojista); um **Supervisor** junta as
respostas. O **Human Escalation Agent** transfere casos para pessoas, e a resposta do humano volta para a mesma conversa
para o agente continuar. Guardrails rodam antes e depois dos agentes, cada passo é rastreado, e tudo é um serviço Docker.

- **Linguagem / stack:** Python 3.13, FastAPI, Google Gemini (LLM + embeddings), busca vetorial numpy (+ híbrida BM25 opcional), estado em SQLite, busca DuckDuckGo
- **Agentes:** Router (+ planejador) · Knowledge · Customer Support · Supervisor · Human Escalation
- **Recursos bônus:** guardrails (injeção direta e indireta), transferência para humano com retorno da resposta, memória
  de conversa, identidade por chave de API, tracing, métricas, dataset de avaliação + LLM-as-judge com outro modelo, CI

> **Versão 2:** atualização de melhorias. A §10 resume o que mudou.

---

## 1. Como rodar

**Requisitos:** Docker (com Compose) e uma chave gratuita da API do Gemini em <https://aistudio.google.com/apikey>.

```bash
git clone <este-repositorio> && cd getnet-multi-agent
cp .env.example .env          # depois edite o .env: defina GEMINI_API_KEY e ADMIN_API_KEY (chave da equipe)
docker compose up --build
```

Na primeira execução, o serviço monta a base de conhecimento: a partir do snapshot versionado
(`data/index_snapshot.json.gz`) quando existe, senão raspando os sites da Getnet (de 1 a 3 minutos; o progresso aparece
nos logs). O índice fica num volume do Docker, então as próximas inicializações são instantâneas, e um índice mais velho
que `INDEX_MAX_AGE_DAYS` é atualizado em segundo plano. `GET http://localhost:8000/health` retorna `"status": "ok"`
quando a base está carregada.

Envie uma mensagem. O lojista é identificado pela **chave de API**; o `user_id` do corpo precisa bater com ela (as
chaves de demonstração estão na §9):

```bash
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer demo-key-cliente1988" \
  -d '{"message": "What is the difference between the Get Clássica and the Get Smart?", "user_id": "cliente1988"}'
```

A resposta traz um `session_id`. Envie-o de volta para continuar a mesma conversa (memória):

```bash
curl -s -X POST http://localhost:8000/chat -H "Content-Type: application/json" \
  -H "Authorization: Bearer demo-key-cliente1988" \
  -d '{"message": "E qual delas imprime comprovante?", "user_id": "cliente1988", "session_id": "<da primeira resposta>"}'
```

Para rodar exatamente o contrato original (sem chave, `user_id` confiado a partir do corpo), defina `AUTH_ENABLED=false`.

**Console web:** <http://localhost:8000/> — uma pequena interface de chat que mostra, para cada resposta, qual agente
atuou, a rota e a confiança, as ferramentas chamadas, as fontes e um trace passo a passo com as latências. Também
permite alternar entre os lojistas fictícios e rodar os cenários de exemplo com um clique.

**Painel de análise:** <http://localhost:8000/dashboard> (chave da equipe) — perguntas mais feitas, perguntas com menor
índice de acerto, perguntas por dia e por rota, indicadores (taxa de sucesso, aprovação dos usuários, taxa de
escalonamento, latência p50/p95/p99, custo estimado por pergunta) e o resultado da última avaliação. As respostas podem
ser avaliadas com 👍/👎 no console; veja a §7.

**Caixa de entrada do atendente humano:** <http://localhost:8000/agent> (chave da equipe) — a fila de transferências. O
especialista lê o resumo e a conversa e responde; o assistente entrega a resposta na conversa do cliente e continua o
atendimento (o console mostra automaticamente). Veja a §5.

Documentação interativa da API (Swagger UI): <http://localhost:8000/docs>

Para rodar todos os cenários de exemplo do desafio e imprimir um resumo legível:

```bash
docker compose exec api python scripts/demo.py
```

### Comandos úteis

| Tarefa | Comando |
|---|---|
| Rodar os testes (offline, sem chave de API; imagem de teste separada) | `docker compose run --rm --build tests` |
| Rodar a avaliação (LLM real, juiz = outro modelo) | `docker compose exec api python -m evals.run_evals --judge` |
| Rodar a avaliação da busca (densa vs. híbrida) | `docker compose exec api python -m evals.run_retrieval_evals` |
| Copiar os relatórios para fora do container (para commitar) | `docker compose cp api:/app/evals/report.md evals/` (também `report.json`, `retrieval_report.md`) |
| Refazer a base de conhecimento (em segundo plano, o índice antigo continua servindo) | `curl -X POST -H "X-Admin-Key: $ADMIN_API_KEY" localhost:8000/admin/reindex` |
| Salvar o índice como snapshot versionado | `docker compose exec api python -m app.rag.ingest --export-snapshot` e depois `docker compose cp api:/app/data/index_snapshot.json.gz data/` |
| Acompanhar os logs estruturados | `docker compose logs -f api` |
| Métricas operacionais | `curl -H "X-Admin-Key: $ADMIN_API_KEY" localhost:8000/metrics` |

Os últimos relatórios commitados estão em [`evals/report.md`](evals/report.md) e
[`evals/retrieval_report.md`](evals/retrieval_report.md).

### Configuração

Todas as configurações são variáveis de ambiente (veja o `.env.example`). As mais relevantes:

| Variável | Padrão | Para que serve |
|---|---|---|
| `GEMINI_API_KEY` | – | Obrigatória |
| `LLM_MODEL` | `gemini-flash-lite-latest` | Modelo dos agentes especialistas |
| `ROUTER_MODEL` | `gemini-flash-lite-latest` | Modelo do roteamento |
| `LLM_THINKING_LEVEL` | `low` | Nível de raciocínio dos modelos com "thinking" |
| `EMBEDDING_MODEL` | `gemini-embedding-001` | Embeddings para o RAG (768 dimensões) |
| `MAX_TOOL_ITERATIONS` | `5` | Limite de voltas no loop de ferramentas de cada agente |
| `ROUTER_CONFIDENCE_THRESHOLD` | `0.55` | Abaixo disso, o pedido vai para um humano |
| `RETRIEVAL_TOP_K` / `RETRIEVAL_MIN_SCORE` | `5` / `0.45` | Quantidade de trechos buscados e piso de relevância |
| `RETRIEVAL_MODE` | `dense` | `dense` ou `hybrid` (densa + BM25, fundidas por RRF); padrão escolhido pela avaliação da busca |
| `ADMIN_API_KEY` | – | Chave da equipe para `/dashboard`, `/agent`, `/metrics`, `/analytics`, `/evals`, `/handoffs` (vazia = desativados) |
| `AUTH_ENABLED` | `true` | Identidade pela chave de API; `false` = confia no `user_id` do corpo |
| `RATE_LIMIT_PER_MINUTE` | `20` | Por lojista e por IP |
| `MEMORY_MAX_TURNS` / `SESSION_TTL_HOURS` | `6` / `24` | Turnos passados aos agentes / tempo parado até abrir nova sessão |
| `MAX_PLAN_STEPS` | `2` | Especialistas que o supervisor pode rodar por mensagem |
| `JUDGE_MODEL` | `gemini-flash-latest` | Juiz da avaliação (modelo diferente do dos agentes) |
| `INDEX_MAX_AGE_DAYS` | `7` | Índice mais velho é atualizado em segundo plano |

Os aliases `-latest` sempre apontam para a versão atual do Gemini. Para fixar uma versão (recomendado em produção,
por reprodutibilidade), defina um id exato de modelo de <https://ai.google.dev/gemini-api/docs/models>.

### Rodando sem Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements.lock -r requirements-dev.lock
cp .env.example .env   # defina GEMINI_API_KEY e ADMIN_API_KEY
uvicorn app.main:app --reload
pytest
```

---

## 2. Arquitetura

```mermaid
flowchart TD
    U([POST /chat<br/>chave de API + message + session_id]) --> AU[Identidade · limite de requisições<br/>user_id vem da chave]
    AU --> GI[Guardrails de entrada<br/>injeção · mascaramento de dados · tamanho]
    GI -- bloqueado --> R0([Resposta segura])
    GI --> MEM[(Memória da conversa<br/>SQLite · últimos N turnos)]
    MEM --> RT{{Router Agent<br/>rota + confiança + plano}}
    RT -- saudação / fora de escopo / inseguro --> R1([Resposta curta do router])
    RT -- "plano: 1 ou 2 passos" --> SUP[Supervisor<br/>roda o plano em ordem]
    RT -- escalonamento ou baixa confiança --> EA[Human Escalation Agent]
    SUP --> SA[Customer Support Agent]
    SUP --> KA[Knowledge Agent]
    SA -- 2 respostas --> SY[Síntese do Supervisor]
    KA -- 2 respostas --> SY
    KA -- escalate_to_human --> EA
    SA -- escalate_to_human --> EA
    KA --- KT[(search_knowledge_base<br/>web_search · dado não confiável)]
    SA --- ST[(perfil · vendas e recebimentos<br/>transações · status da maquininha<br/>ticket de suporte · central de ajuda)]
    EA --- ET[(resumo · prioridade<br/>ticket HUM- ligado à sessão)]
    EA -.-> HQ[[Caixa do atendente /agent]]
    HQ -. resposta .-> RES[Agente continua a conversa]
    RES -.-> MEM
    KA --> GO[Guardrails de saída]
    SA --> GO
    SY --> GO
    EA --> GO
    GO --> RESP([JSON: resposta · session_id · agente · rota · fontes<br/>ferramentas · tickets · trace])
```

### Os agentes

| Agente | Responsabilidade | Como funciona | Ferramentas |
|---|---|---|---|
| **Router** (+ planejador) | Porta de entrada. Decide quem responde a mensagem e em que ordem. | Uma chamada ao LLM com schema JSON → `route`, `confidence`, `language`, `reason`, `reply`, `plan`. Vê os últimos turnos da conversa. Validação defensiva: uma saída inválida vira uma decisão com confiança 0; o plano só aceita especialistas conhecidos, sem repetição, começando pela rota. | – |
| **Knowledge** | Produtos e serviços da Getnet; perguntas gerais em tempo real. | Loop com ferramentas. Busca primeiro na base de conhecimento, recorre à web se precisar e responde só com o que as ferramentas trouxeram. | `search_knowledge_base`, `web_search`, `escalate_to_human` |
| **Customer Support** | A conta do próprio lojista: depósitos, transações, recusas, maquininhas. | Loop com ferramentas sobre os dados do lojista; usa a central de ajuda para procedimentos oficiais; abre tickets quando a solução de problemas não basta. | `get_customer_profile`, `get_sales_and_settlements`, `get_recent_transactions`, `get_terminal_status`, `open_support_ticket`, `search_knowledge_base`, `escalate_to_human` |
| **Supervisor** | Combina os especialistas quando o plano tem dois passos. | Roda os passos em ordem com um contexto compartilhado; se um passo pede um humano, os passos restantes são descartados e o caso é transferido (replanejamento); com duas respostas, uma chamada de síntese junta as duas usando só o que os especialistas encontraram. | – |
| **Human Escalation** (bônus) | Transferência para um especialista humano. | O LLM gera um resumo do caso (com a conversa inteira), a prioridade e a categoria; depois um ticket `HUM-` é criado, ligado à sessão, e o usuário recebe um número de protocolo. Uma transferência aberta por conversa. Usa um modelo de mensagem pronto se o LLM falhar. | cria tickets `HUM-` |

### Comunicação entre os agentes

O **orquestrador** (`app/orchestrator.py`) é dono do fluxo. Os agentes se comunicam por **chamadas de função diretas e
síncronas**, que compartilham um `ToolContext` da requisição (id do usuário, trace, fontes coletadas, tickets, sinal de
escalonamento).

- Os especialistas nunca chamam uns aos outros. Quando um decide que precisa de um humano, ele chama a ferramenta
  `escalate_to_human`, que só levanta um sinal; o orquestrador então roda o Escalation Agent. Todas as decisões de
  roteamento ficam num lugar só.
- O conhecimento é compartilhado por ferramentas: o Support Agent usa a mesma `search_knowledge_base` do Knowledge Agent,
  então "minha maquininha não conecta" combina o diagnóstico em tempo real com o procedimento oficial da central de ajuda.
- **Planos.** Quando uma mensagem precisa de dois especialistas ("meu dinheiro de ontem não caiu e qual é a regra do
  D+1?"), o Router devolve `plan: [support, knowledge]` com uma tarefa curta para cada um. O orquestrador roda os dois em
  ordem no mesmo contexto (o segundo passo vê as fontes e os tickets do primeiro) e o Supervisor escreve uma resposta
  só. O plano é limitado por `MAX_PLAN_STEPS`, então custo e latência ficam sob controle.
- **O estado** fica fora dos agentes: a conversa (SQLite, `app/memory.py`) é carregada antes do roteamento e gravada
  depois da resposta, então os agentes continuam sem estado e um restart não perde nada.

Escolhi chamadas diretas em vez de uma fila de mensagens porque cada interação é um pedido e uma resposta curtos: é o
modelo mais fácil de rastrear, testar e entender. O retorno da resposta humana é assíncrono, mas não precisa de fila: a
transferência fica gravada, e a resposta retoma a conversa a partir do estado salvo. Uma fila passa a valer a pena com
agentes demorados (veja a §8).

### Exemplo do fluxo de uma mensagem

`"When will the money from yesterday's sales be deposited?"` enviada pelo `cliente1988`:

1. **Guardrails de entrada:** sem injeção e sem dado sensível → segue sem alteração.
2. **Router:** `{"route": "support", "confidence": 0.95, "language": "en"}`.
3. **Support Agent, iteração 1:** o modelo chama `get_sales_and_settlements(period="yesterday")`. A ferramenta lê os dados
   do **cliente1988** (o id do usuário vem da requisição, nunca do modelo) e devolve cada venda com as datas previstas
   de depósito: débito D+1, Pix no mesmo dia, crédito D+30, parcelado a cada 30 dias.
4. **Support Agent, iteração 2:** o modelo escreve a resposta com esses números.
5. **Guardrails de saída:** mascaram qualquer dado sensível que o modelo tenha repetido.
6. **Resposta:** o texto + `session_id`, `agent`, `route`, `tools_used`, `sources`, `tickets` e o `trace` completo, com
   a latência e o uso de tokens de cada passo. O turno fica gravado na sessão.

`"Meu dinheiro de ontem não caiu e quero saber a regra do D+1"` (pergunta mista):

1. **Router:** `route: support`, `plan: [{support: "verificar os depósitos de ontem"}, {knowledge: "explicar a regra do D+1"}]`.
2. O **Support Agent** lê as vendas e as datas previstas de depósito; o **Knowledge Agent** busca a regra no site.
3. O **Supervisor** responde uma vez: primeiro os depósitos do próprio lojista, depois a regra, com as fontes do site.
4. `agent` = `support+knowledge+supervisor`, e o trace mostra cada passo.

---

## 3. Pipeline de RAG

```
data/sources.txt ──► raspagem ──► limpeza ──► chunks ──► descarta chunks envenenados ──► embeddings ──► storage/index.json
                                                                                                │
pergunta ──► densa (cosseno) [+ BM25 + RRF no modo híbrido] ──► piso de relevância ──► marcado como não confiável ──► resposta + fontes
```

| Etapa | Implementação | Por quê |
|---|---|---|
| **Ingestão** | `app/rag/ingest.py` raspa as URLs de `data/sources.txt` (getnet.net + site.getnet.com.br: maquininhas, Pix, crediário, link de pagamento, conta digital, empréstimo, central de ajuda) e segue os links do mesmo domínio um nível abaixo (máx. 60 páginas). Sequencial, 0,5 s de pausa, User-Agent identificado, só páginas públicas. Do getnet.net fica só a edição em inglês; páginas jurídicas, de imprensa e blog são ignoradas. | O desafio pediu fontes além da URL sugerida. Seguir um nível de links pega as páginas de produto sem precisar listar todas. As edições por país do getnet.net (Portugal, Espanha, México…) descrevem produtos e taxas de outros mercados e dariam respostas erradas a lojistas brasileiros. |
| **Limpeza** | BeautifulSoup remove scripts, menus, cabeçalho, rodapé e banners de cookies; descarta fragmentos muito curtos e linhas repetidas. | Menus e banners repetidos em todas as páginas dominariam a busca. |
| **Chunks** | Corte respeitando parágrafos, ~1000 caracteres com 150 de sobreposição; o título da página vai no início de cada chunk; chunks repetidos entre páginas são removidos. | Respeitar parágrafos mantém as ideias inteiras; a sobreposição preserva respostas que caem na fronteira; o título diz ao modelo de que produto o trecho fala. |
| **Embeddings** | `gemini-embedding-001`, 768 dimensões, `RETRIEVAL_DOCUMENT` para os chunks e `RETRIEVAL_QUERY` para as perguntas. | Multilíngue: perguntas em inglês encontram conteúdo em português. Tipos de tarefa assimétricos melhoram a qualidade da busca. |
| **Armazenamento** | `app/rag/store.py`: vetores normalizados em numpy, salvos como JSON num volume do Docker. | Para algumas centenas de chunks, a busca exata por força bruta leva bem menos de 1 ms e não exige infraestrutura. A interface (`add` / `search` / `save` / `load`) corresponde diretamente a Chroma, pgvector ou Qdrant, se a base crescer. |
| **Busca (densa, híbrida opcional)** | Ranking denso (cosseno) + ranking esparso (BM25, `app/rag/bm25.py`, mantém tokens como `d+1`) fundidos por **Reciprocal Rank Fusion** (k = 60), top 5. Piso de relevância: cosseno ≥ 0,45, ou ≥ 0,35 para o top 3 do BM25 (casamento exato forte). Disponível com `RETRIEVAL_MODE=hybrid`; **o padrão é a densa**, porque na avaliação da busca a híbrida não superou a densa neste conteúdo (Recall@5 0,93 nas duas; MRR@5 0,88 densa vs. 0,82 híbrida). | Embeddings captam o sentido; o BM25 pega nomes exatos de produtos e códigos que os embeddings borram. O RRF usa só as posições, então as duas escalas de pontuação nunca precisam ser calibradas. Trechos irrelevantes são piores que nenhum: o piso mantém "não encontrei" para perguntas sem relação. |
| **Conteúdo não confiável** | Frases com cara de instrução são removidas e cada chunk / trecho da web vai dentro de `<untrusted_content>`; os prompts dizem que resultado de ferramenta é dado, nunca instrução. Chunks com padrões de injeção são descartados na ingestão. | Prompt injection indireto: uma página da web não pode dar ordens ao agente. |
| **Atualização** | `built_at` nos metadados do índice; o `/health` mostra a idade; mais velho que `INDEX_MAX_AGE_DAYS` → reconstrução em segundo plano com troca atômica (uma raspagem que falha mantém o índice antigo); `POST /admin/reindex` sob demanda. Um snapshot versionado deixa a primeira subida determinística. | O site muda; o índice não pode envelhecer em silêncio, e uma atualização nunca pode derrubar o serviço. |
| **Métricas da busca** | `evals/run_retrieval_evals.py`: Recall@5 (taxa de acerto), MRR@5, Precision@5 e taxa de busca vazia, densa vs. híbrida, em 14 consultas rotuladas. | Mede a busca separada da geração, para saber se uma resposta ruim veio de "não achou" ou de "achou e usou mal". |
| **Geração** | O Knowledge Agent chama `search_knowledge_base` (pode reformular e buscar de novo) e responde só com os resultados. As fontes voltam num campo `sources` separado. | As regras de fundamentação ficam no prompt; as citações permitem que usuários e avaliadores confiram a resposta. |

---

## 4. Como as ferramentas do LLM são usadas

- **Function calling nativo.** Cada ferramenta é um schema JSON (o que o modelo vê) mais uma função Python (o que roda).
  O loop do agente em `app/agents/base.py` executa as ferramentas manualmente, em vez de usar o modo automático do SDK,
  para que cada chamada seja registrada e rastreada com seus argumentos e resultado.
- **Saída estruturada** (schema JSON) no Router e no Escalation Agent: as decisões são dados validados, não texto a interpretar.
- **A escolha do modelo é configuração:** router e agentes podem usar modelos diferentes. Comparei o modelo Flash maior
  com o Flash-Lite nos cenários de exemplo: com a base de conhecimento fazendo o trabalho pesado, o Flash-Lite deu
  respostas igualmente fundamentadas em ~7 s contra ~50 s (o modelo maior também teve erros 504 de prazo no plano
  gratuito), por isso é o padrão. O nível de raciocínio fica em `low` pelo mesmo motivo.
- **Segurança desde o projeto:**
  - Nenhuma ferramenta aceita o parâmetro `user_id`. O id é injetado a partir da requisição, então nenhum prompt
    consegue fazer o modelo ler os dados de outro lojista.
  - Argumentos que não estão no schema são descartados antes da execução.
  - Ferramentas desconhecidas e exceções viram resultados de erro dos quais o modelo pode se recuperar; nunca derrubam
    a requisição.
  - O loop é limitado por `MAX_TOOL_ITERATIONS`. Atingir o limite leva a um humano, em vez de falhar em silêncio.
- **Abstração do fornecedor.** Os agentes dependem do protocolo `LLMClient` (`app/llm.py`), não do SDK do Gemini.
  Adicionar outro fornecedor é escrever uma classe. A mesma separação permite que os testes usem um modelo falso.
- **Ferramentas de leitura vs. escrita.** Ferramentas que mudam algo (`open_support_ticket`) são marcadas com
  `side_effect=True`: no máximo uma escrita por turno, e a escrita é **idempotente** — um ticket aberto por lojista +
  categoria em 24 h; uma repetição devolve o ticket existente (`deduplicated: true`). As transferências são idempotentes
  por conversa.
- **Confiabilidade:** timeout por requisição, novas tentativas com espera crescente em erros 429/5xx e um **circuit
  breaker**: depois de 5 falhas seguidas, as chamadas ao LLM falham na hora por 30 s (o estado aparece no `/health`), em
  vez de fazer cada usuário esperar timeouts.

---

## 5. Guardrails e transferência para humano (bônus)

**Identidade e acesso** (`app/security.py`):

- o lojista é identificado pela **chave de API** (`Authorization: Bearer`); as chaves ficam guardadas como hash sha256
  em `data/api_keys.json`. O `user_id` do corpo precisa bater com a chave (senão, 403), então trocar o corpo não lê mais
  os dados de outro lojista. Uma mensagem que cita a conta de outro lojista é recusada antes de qualquer chamada ao LLM.
  Em produção, é aqui que o token OAuth2/OIDC do login da Getnet seria validado;
- uma sessão pertence a um lojista só; só quem perguntou pode avaliar a resposta;
- os endpoints operacionais e do atendente humano exigem a **chave da equipe** (`X-Admin-Key`, comparação em tempo constante);
- **limite de requisições** por lojista e por IP (429 + `Retry-After`).

**Entrada** (`app/guardrails.py`, determinístico e testado):

- padrões de prompt injection / jailbreak (em inglês e português) são bloqueados antes de qualquer chamada ao LLM;
- **injeção indireta:** o conteúdo que os agentes leem (base de conhecimento, web) é limpo e marcado como dado não confiável (§3);
- números de cartão (validados pelo algoritmo de Luhn, para não afetar números de pedido), CVV, senhas, CPF e CNPJ
  são **mascarados antes de chegar ao LLM ou aos logs**, e o usuário é orientado a não compartilhá-los;
- mensagens vazias ou grandes demais são recusadas.

**Camada semântica:** o Router classifica como `out_of_scope` (ex.: "escreva um script para mim") ou `unsafe` e
responde dizendo com o que o assistente *pode* ajudar.

**Saída:** dados sensíveis são mascarados de novo na resposta final. Os prompts proíbem revelar instruções ou inventar
taxas, preços ou dados.

**A transferência para humano** acontece quando:

- o usuário pede um humano ou relata fraude, chargeback ou questão jurídica (Router → `escalation`);
- a confiança do Router fica abaixo do limite;
- um especialista chama `escalate_to_human`;
- um agente atinge o limite de iterações.

O Escalation Agent escreve um resumo do caso para a equipe humana (com a conversa inteira), define prioridade e
categoria, cria um ticket `HUM-` ligado à sessão e informa ao usuário o número do protocolo.

**Humano no circuito, o ciclo completo:** agente → humano → resposta → agente continua.

1. O caso aparece na **caixa do atendente** (`/agent`, ou `GET /handoffs`) com resumo, prioridade e conversa.
2. O especialista responde (`POST /handoffs/{ticket}/reply`, podendo encerrar o caso).
3. A resposta é gravada na conversa e o agente especialista que estava com o caso **escreve a próxima mensagem ao
   cliente a partir dela** (pode chamar ferramentas para complementar). Se o LLM falhar, a resposta do humano é entregue
   como está.
4. O cliente vê a resposta na mesma conversa (`GET /sessions/{id}/messages`; o console consulta enquanto há transferência
   aberta) e continua conversando: o próximo turno tem a resposta do humano no contexto.

---

## 6. Estratégia de testes

Testar um sistema com LLM significa separar **o que precisa ser sempre verdade** (o fluxo) de **quão bom o modelo é**
(as respostas). Cada um exige ferramentas diferentes.

| Camada | O que verifica | Onde | Precisa de chave? |
|---|---|---|---|
| **Unitária** | Guardrails (mascaramento, injeção, Luhn), chunks, limpeza do HTML, busca vetorial e persistência, cálculo das datas de depósito, códigos de recusa, interpretação da saída do router | `tests/test_units.py` | Não |
| **Integração (orquestração)** | Fluxo completo com um `FakeLLM` roteirizado: agente certo por cenário, ferramentas esperadas, fontes retornadas, caminhos de escalonamento (explícito, baixa confiança, pedido pelo especialista, limite do loop), injeção sem chegar ao LLM, mascaramento de ponta a ponta, falhas da web e do LLM tratadas com elegância, isolamento do user id, trace e métricas registrados | `tests/test_orchestration.py` | Não |
| **Contrato da API** | Formato da resposta, validação do payload (erros 422), `/health`, `/metrics` | `tests/test_api.py` | Não |
| **Comportamento v2** | Fronteira de identidade (401/403, `user_id` trocado), endpoints da equipe, limite de requisições, memória entre turnos e após restart, dono da sessão, ciclo da resposta humana, transferências e tickets idempotentes, limite de escrita, plano de dois passos + síntese, replanejamento para humano, injeção indireta (busca, web, ingestão), preferência por domínios oficiais, BM25/busca híbrida, circuit breaker | `tests/test_v2.py` | Não |
| **Avaliação (qualidade)** | Modelo real num dataset rotulado: acerto de rota, acerto na escolha de ferramentas, verificação de conteúdo, groundedness e helpfulness por LLM-as-judge, latência e tokens | `evals/` | Sim |

O `FakeLLM` (`tests/fakes.py`) implementa a mesma interface do cliente Gemini: roteia por palavras-chave, chama a
ferramenta que o cenário precisa e responde repetindo os resultados das ferramentas. Isso deixa os testes de integração
**determinísticos, offline e rápidos** (87 testes em cerca de um segundo), para rodar a cada commit: o **GitHub
Actions** (`.github/workflows/ci.yml`) instala as dependências travadas por hash, roda a suíte, monta a imagem de teste e
confere que a imagem de produção não tem testes nem pytest dentro. Um teste de
integração falhando significa que a orquestração quebrou, e não que o modelo teve um dia ruim.

**Testes de integração completos da orquestração** ampliam isso em quatro direções:

1. **Matriz de cenários.** Para cada rota × desfecho (respondido, erro de ferramenta, busca vazia, cliente desconhecido,
   escalonamento), verificar pelo trace o caminho dos agentes, a sequência de ferramentas e o contrato da resposta.
2. **Testes de contrato por ferramenta.** O schema e o formato da saída de cada ferramenta ficam fixados, para que uma
   mudança na camada de dados não quebre em silêncio o que o modelo recebe.
3. **Execuções reais gravadas.** Gravar as respostas reais do Gemini uma vez e reproduzi-las no CI (estilo VCR). Isso
   pega regressões no tratamento de prompts e respostas sem pagar a cada execução.
4. **Portões de avaliação.** O `evals/run_evals.py` roda a cada mudança de prompt ou de modelo. Uma queda no acerto de
   rota ou na groundedness em relação ao relatório anterior bloqueia a mudança. O dataset cresce com cada falha de
   produção corrigida (testes de regressão de comportamento).

---

## 7. Observabilidade, avaliação e monitoramento em produção (bônus)

**O que está implementado**

- **Traces:** cada requisição recebe um `trace_id`. Cada passo (guardrails, decisão do router, cada iteração do LLM,
  cada chamada de ferramenta com argumentos e resultado, escalonamento) é um span com latência e uso de tokens. O trace
  volta na resposta da API e é registrado nos logs.
- **Logs estruturados:** uma linha JSON por span (`trace_id`, `user_id`, `span`, `duration_ms`, atributos), prontos para
  Loki, ELK ou Datadog. Dados sensíveis são mascarados antes de chegar aos logs.
- **Métricas:** `GET /metrics` expõe requisições, erros, rotas, bloqueios dos guardrails, escalonamentos, contagem de
  chamadas por ferramenta, tokens, custo estimado, sinais de segurança (ex.: texto de injeção removido) e latência
  p50/p95/p99 por rota.
- **Avaliação:** um dataset rotulado (`evals/dataset.jsonl`: os 10 cenários do desafio mais casos em português,
  escalonamento, fora de escopo, injeção, cliente desconhecido, **memória em vários turnos**, **pergunta mista (dois
  especialistas)** e **ataques**: um jailbreak em português e um pedido de dados de outro lojista) e um script que gera
  o `evals/report.md` e o `report.json` (commitados) e a cópia que o painel mostra, com os modelos e a versão dos prompts.
  - Casos que esperam nenhuma ferramenta falham se alguma for chamada (recusas e transferências não podem tocar dados).
  - O **juiz é outro modelo** (`JUDGE_MODEL`, por padrão o Flash maior) diferente do dos agentes (Flash-Lite), e recebe
    os resultados **completos** das ferramentas de cada turno. Um juiz de outro fornecedor e uma revisão humana mensal
    de uma amostra das respostas julgadas, para medir a concordância, são os próximos passos de calibração.
- **Custo:** tokens por requisição e um custo estimado (preços de tabela, configuráveis) no `/metrics` e no painel.
- **Análise das conversas** (`app/analytics.py`, SQLite no volume de armazenamento): cada requisição é guardada com o
  resultado (só a mensagem já mascarada). O **sucesso** de uma pergunta é o 👍/👎 do usuário, quando ele avalia; senão,
  é a **resolução automática** — respondida sem erro, escalonamento ou bloqueio, e fundamentada (fontes retornadas ou
  dados do lojista consultados). O `GET /dashboard` mostra as perguntas mais feitas, as de menor acerto e as
  tendências, para a equipe saber quais assuntos precisam de conteúdo ou prompts melhores.

**Como eu operaria em produção**

| Sinal | Ferramentas | Alertar quando |
|---|---|---|
| Latência p95, taxa de erro, 429/5xx do LLM | OpenTelemetry → Prometheus / Grafana | p95 > 8 s ou taxa de erro > 2% por 5 min |
| Custo | tokens por requisição e por rota | gasto diário acima do orçamento, ou salto de tokens por requisição (loops, prompt inchado) |
| Saúde do roteamento | distribuição das rotas, histograma de confiança do router | mudança brusca na distribuição ou muitas rotas com baixa confiança |
| Taxa de escalonamento | escalonamentos / requisições | > X% (o assistente está falhando) ou ≈ 0 (transferência quebrada) |
| Qualidade da busca | similaridade do 1º resultado, % de buscas vazias | aumento de buscas vazias (índice desatualizado ou site mudou) |
| Qualidade das respostas | LLM-as-judge noturno numa amostra de conversas reais; joinha para cima/para baixo | queda da nota de groundedness |
| Segurança | bloqueios dos guardrails por tipo | pico de tentativas de injeção |

Ferramentas de tracing de agentes como Langfuse ou LangSmith se encaixam diretamente sobre os spans existentes.

---

## 8. Decisões de projeto, trade-offs e limitações

- **Sem framework de agentes.** A orquestração tem cerca de 300 linhas de Python puro. Com cinco agentes e um fluxo
  claro, um framework adicionaria abstração sem adicionar capacidade, e aqui cada passo é explícito e testável. LangGraph
  seria minha escolha se o fluxo virasse um grafo de verdade com idas e vindas entre agentes; a pausa/retomada humana e
  o estado persistente necessários aqui cabem num pequeno armazenamento de estado.
- **Gemini.** Plano gratuito e um único fornecedor para LLM e embeddings. A abstração `LLMClient` mantém a escolha reversível.
- **Vector store em numpy** em vez de um banco vetorial: exato, rápido e sem nada para operar nessa escala (veja a §3).
- **Busca DuckDuckGo** não exige chave de API. Os resultados da web (clima, câmbio) são trechos aproximados, e o agente
  diz de onde vieram. Em produção, eu usaria APIs dedicadas de clima e câmbio.
- **Dados de cliente fictícios.** O `data/customers.json` é fictício e usa datas relativas, então "vendas de ontem"
  sempre funciona. As regras de recebimento são simplificadas (dias úteis sem feriados).
- **Memória em SQLite.** Certo para uma réplica; com várias réplicas, sessões e transferências vão para Postgres/Redis
  atrás da mesma interface `ConversationStore`. O mesmo vale para o arquivo JSON de tickets e o limite de requisições em memória.
- **Planos são sequenciais e limitados a dois passos.** Basta para as perguntas mistas reais; um planejador geral (idas e
  vindas entre agentes) é onde um framework de grafo como o LangGraph começaria a compensar.
- **Atualização da base.** Atualização por idade. Em produção, um job agendado compararia hashes das páginas e geraria
  embeddings só do que mudou.

**Evolução para produção:** OAuth2/OIDC no lugar das chaves de API, Postgres/Redis para o estado, fila para agentes
demorados, banco vetorial gerenciado com reingestão incremental, CI com respostas gravadas, juiz de outro fornecedor com
calibração humana e uma avaliação canário antes de cada mudança de prompt ou de modelo.

---

## 9. Estrutura do projeto

```
app/
  main.py               App FastAPI: /chat, /sessions, /feedback, /handoffs, /agent, /dashboard, /analytics, /evals, /health, /metrics
  security.py           Identidade por chave de API, chave da equipe, limite de requisições
  memory.py             Estado da conversa: sessões, turnos, transferências para humano (SQLite)
  resilience.py         Circuit breaker do fornecedor de LLM
  analytics.py          Análise das conversas: registro das perguntas, avaliações, sucesso por pergunta
  static/index.html     Console web (chat + caminho dos agentes, ferramentas, fontes, trace, 👍/👎, respostas humanas)
  static/dashboard.html Painel de análise
  static/agent.html     Caixa do atendente humano (fila de transferências + resposta)
  orchestrator.py       Fluxo: guardrails → memória → router/plano → especialistas → supervisor → escalonamento → saída
  config.py             Configurações (variáveis de ambiente)
  llm.py                Interface LLMClient + implementação Gemini (novas tentativas, timeouts)
  guardrails.py         Guardrails de entrada e saída
  observability.py      Traces, logs estruturados, métricas
  agents/
    prompts.py          Todos os system prompts e schemas de saída (versionados)
    base.py             Loop do agente com ferramentas
    router.py           Router Agent
    specialists.py      Agentes Knowledge, Customer Support, Supervisor e Human Escalation
  tools/                Definição das ferramentas (conhecimento, suporte, escalonamento)
  rag/                  ingest.py (raspagem → chunks → embeddings) · store.py · bm25.py · retriever.py (híbrida + RRF)
  data/                 customer_repository.py (acesso aos dados fictícios + tickets)
data/
  sources.txt           URLs de partida da base de conhecimento
  customers.json        Lojistas fictícios (cliente1988, cliente2024)
  api_keys.json         Chaves de API de demonstração (em hash)
tests/                  Testes unitários, de integração, da API e da v2 (offline, LLM falso)
evals/                  Datasets, scripts e os relatórios commitados (report.md, retrieval_report.md)
.github/workflows/      CI: testes + imagens Docker
requirements.lock       Dependências travadas por hash (requirements-dev.lock para o pytest)
scripts/demo.py         Roda os cenários do desafio contra a API
```

Usuários fictícios para teste: **`cliente1988`** (padaria em Porto Alegre, Get Smart offline com Wi-Fi fraco,
transações recusadas com os códigos 51 e 54, vendas ontem em débito, crédito, parcelado em 3× e Pix) e **`cliente2024`**
(loja de autopeças, com antecipação automática). Qualquer outro id se comporta como cliente desconhecido.

Chaves de API de demonstração (fictícias, só para este desafio): `demo-key-cliente1988`, `demo-key-cliente2024` e
`demo-key-unknown` (um login válido sem conta de lojista). O console entra com elas automaticamente.

---

## 10. Histórico de versões

| Área | v1 | v2 | Onde |
|---|---|---|---|
| Orquestração | Router → um especialista | O Router devolve um plano (1–2 especialistas); o supervisor executa e junta as respostas; um passo que precisa de humano replaneja para a transferência | `orchestrator.py`, `agents/router.py`, `agents/specialists.py` |
| Memória | Uma pergunta por vez | Sessões e turnos em SQLite no volume; últimos N turnos para o router e os especialistas; sobrevive a restart | `memory.py` |
| Transferência para humano | Termina num ticket `HUM-` | Caixa do atendente + endpoint de resposta; a resposta entra na conversa e o agente continua | `orchestrator.resume_after_human`, `static/agent.html` |
| Identidade e acesso | `user_id` vindo do corpo | Identidade pela chave de API (o corpo precisa bater), chave da equipe nos endpoints operacionais, limite de requisições | `security.py`, `main.py` |
| Busca | Só densa, sem medição | Métricas de busca (Recall@5 / MRR@5 / Precision@5); híbrida BM25 + RRF implementada e comparada com a densa (densa mantida como padrão) | `rag/bm25.py`, `rag/retriever.py`, `evals/run_retrieval_evals.py` |
| Injeção indireta | – | Conteúdo da web e da base limpo e marcado como não confiável; chunks envenenados descartados na ingestão; domínios oficiais preferidos | `guardrails.py`, `tools/knowledge_tools.py`, `rag/ingest.py` |
| Atualização do índice | Montado uma vez | Idade no `/health`, atualização em segundo plano com troca atômica, `/admin/reindex`, suporte a snapshot | `main.py`, `rag/ingest.py` |
| Avaliação | 15 casos, juiz da mesma família | 20 casos (vários turnos, mistos, ataques), juiz com outro modelo, checagem de ferramentas mais rígida, relatórios commitados | `evals/` |
| Ferramentas de escrita | Sem limites | Ferramentas `side_effect`, uma escrita por turno, tickets e transferências idempotentes | `tools/base.py`, `data/customer_repository.py` |
| Confiabilidade e custo | Novas tentativas | Circuit breaker, custo estimado, p99 | `resilience.py`, `observability.py` |
| Build e CI | Imagem única, sem hashes | Dependências travadas por hash, imagem multi-stage sem testes, GitHub Actions | `Dockerfile`, `requirements*.lock`, `.github/workflows/ci.yml` |

---

## Notas de desenvolvimento

Construí este projeto com o auxílio do Claude Opus 5.5 como assistente de programação. Usei o Gemini como LLM e o
VirtualBox para a virtualização: tudo roda dentro de uma máquina virtual Ubuntu Server LTS, em um container Docker.

Configurei o redirecionamento de portas para acessar a interface web pelo navegador da máquina física, e o SSH pelo
PowerShell para administrar a VM pela linha de comando.

Tive alguns problemas, como timeouts no Gemini, porque o plano gratuito não tinha o desempenho esperado (resolvido com a
mudança para o plano pago), e problemas no RAG com páginas no mesmo idioma, mas de outro país, como Portugal (corrigi
limitando as páginas buscadas).

Também testei trocar a busca do RAG de densa para híbrida, mas o resultado foi pior na avaliação; por isso voltei para a
busca densa, que pontuou melhor.
