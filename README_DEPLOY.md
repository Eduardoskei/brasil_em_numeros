# Deploy no Render

## O que mudou em relação ao notebook do Colab

| Colab | Render |
|---|---|
| `userdata.get("DATABASE_URL")` | variável de ambiente `DATABASE_URL` |
| `userdata.get("NGROK_KEY")` + `pyngrok` | não é mais necessário — o Render já expõe uma URL pública |
| `nest_asyncio.apply()` + `await server.serve()` | removido — quem sobe o servidor é o **Start Command** do Render |
| Testes/prints soltos no fim do notebook | removidos do `main.py` (não fazem sentido em produção); veja `# Testes` abaixo se quiser rodá-los localmente |

## Passo a passo

1. **Suba estes arquivos para um repositório no GitHub** (`main.py`, `requirements.txt`, e opcionalmente `render.yaml`).

2. **Banco de dados**: você precisa de um Postgres acessível pela internet.
   - Mais simples: crie um **Postgres no próprio Render** (New > PostgreSQL). Ele te dá uma "Internal Database URL" (se a API e o banco estiverem na mesma região) ou "External Database URL".

3. **Crie o Web Service no Render**:
   - New > Web Service > conecte o repositório.
   - Runtime: Python 3
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Em **Environment**, adicione a variável `DATABASE_URL` com a connection string do passo 2 (inclua `?sslmode=require` se sua string ainda não tiver — o código já força `sslmode="require"` na conexão, então a URL crua do Render funciona).

   Se preferir, use o `render.yaml` incluído (Render > New > Blueprint) e ele já configura build/start command automaticamente — só falta preencher `DATABASE_URL` no painel.

4. **Deploy**. No primeiro request (ou no boot, via `lifespan`), a API busca os dados no IBGE e os cacheia no Postgres (`ibge_cache`). Isso evita bater na API do IBGE toda vez que o serviço reinicia.

5. **Teste os endpoints**:
   - `GET /health` → checagem simples
   - `GET /grafico?indicador=populacao&regiao=brasil`

## Observações

- **Plano free do Render "dorme"** o serviço após um tempo sem tráfego; o próximo request vai demorar um pouco mais (precisa rebuscar/reconectar). Isso é normal.
- O CORS está liberado para `*` (`allow_origins=["*"]`) — restrinja para o domínio do seu front-end quando for para produção de verdade.
- Os testes/prints que existiam no fim do notebook (seção "Testes") foram deixados de fora do `main.py` porque rodavam automaticamente na importação do módulo — no Render isso rodaria toda vez que o serviço subisse. Se quiser rodá-los, crie um script separado (`teste_local.py`) que importa as funções de `main.py` e roda-os manualmente.
