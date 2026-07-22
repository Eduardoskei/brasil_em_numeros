import os
import json
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any

import pandas as pd
import plotly.express as px
import requests
from psycopg2 import pool as pg_pool
from psycopg2.extras import Json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuração / Banco de dados
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "Variável de ambiente DATABASE_URL não definida. "
        "Configure-a em Render > seu serviço > Environment."
    )

db_pool = pg_pool.SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=DATABASE_URL,
    sslmode="require",
)


def get_conn():
    return db_pool.getconn()


def put_conn(conn):
    db_pool.putconn(conn)


def init_cache_table():
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ibge_cache (
                    cache_key   TEXT PRIMARY KEY,
                    payload     JSONB NOT NULL,
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
    finally:
        put_conn(conn)


def cache_get(key: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM ibge_cache WHERE cache_key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        put_conn(conn)


def cache_set(key: str, payload) -> None:
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ibge_cache (cache_key, payload, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (cache_key)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = now();
                """,
                (key, Json(payload)),
            )
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Ingestão
# ---------------------------------------------------------------------------

def buscar_estados() -> list[dict[str, Any]]:
    """Busca a lista de todos os estados do IBGE."""
    cache_key = "estados"

    cached = cache_get(cache_key)
    if cached is not None:
        print("Carregando estados do cache (Postgres)...")
        return cached

    url = "https://servicodados.ibge.gov.br/api/v1/localidades/estados"

    try:
        response = requests.get(url, timeout=(3, 10))
        response.raise_for_status()
        data = response.json()

        cache_set(cache_key, data)

        print("Estados buscados com sucesso e salvos no cache!")
        return data

    except requests.RequestException as error:
        print(f"Falha ao buscar estados: {error}")
        return []


def buscar_indicador(agregado_id: int, localidade_ids) -> dict[str, Any] | None:
    """Função genérica para buscar qualquer indicador do SIDRA/IBGE."""
    estados_str = ",".join(str(i) for i in localidade_ids)

    meta_url = f"https://servicodados.ibge.gov.br/api/v3/agregados/{agregado_id}/metadados"
    try:
        meta_resp = requests.get(meta_url, timeout=(3, 10))
        meta_resp.raise_for_status()
        variavel_id = meta_resp.json()["variaveis"][0]["id"]
    except requests.RequestException as error:
        print(f"Falha ao buscar metadados do agregado {agregado_id}: {error}")
        return None

    cache_key = f"agregado_{agregado_id}_var{variavel_id}_{estados_str.replace(',', '-')}"

    cached = cache_get(cache_key)
    if cached is not None:
        print(f"Carregando indicador {agregado_id} do cache (Postgres)...")
        return cached

    url = (
        f"https://servicodados.ibge.gov.br/api/v3/agregados/"
        f"{agregado_id}/periodos/-1/variaveis/{variavel_id}?localidades=N3[{estados_str}]"
    )

    try:
        response = requests.get(url, timeout=(3, 10))
        response.raise_for_status()
        data = response.json()

        cache_set(cache_key, data)
        print(f"Indicador {agregado_id} buscado com sucesso e salvo no cache!")
        return data

    except requests.RequestException as error:
        print(f"Falha ao buscar indicador {agregado_id}: {error}")
        return None


# ---------------------------------------------------------------------------
# Limpeza
# ---------------------------------------------------------------------------

def indicadores_para_df(payload):
    series = payload[0]["resultados"][0]["series"]
    linhas = []

    for s in series:
        nome = s["localidade"]["nome"]
        uf_id = int(s["localidade"]["id"])
        ano = max(s["serie"].keys())
        valor = s["serie"][ano]

        # Tratar faltantes
        if valor in ("...", "-", "X", "..", ""):
            valor = None
        else:
            try:
                valor = float(str(valor).replace(".", "").replace(",", ".")) \
                    if "," in str(valor) else float(valor)
            except (TypeError, ValueError):
                valor = None

        linhas.append({"uf_id": uf_id, "nome": nome, "valor": valor})

    df = pd.DataFrame(linhas)
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce").astype(float)
    df = df.dropna(subset=["valor"]).reset_index(drop=True)

    assert df["valor"].dtype.kind == "f", "'valor' precisa ser float"
    assert df["valor"].isna().sum() == 0, "ainda há linhas sem valor"

    return df


def mesclar_regiao(df, estados):
    if df.empty or not estados:
        df["regiao"] = pd.Series(dtype="object")
        return df

    mapa_regiao = {int(e["id"]): e["regiao"]["sigla"] for e in estados}
    df = df.copy()
    df["regiao"] = df["uf_id"].map(mapa_regiao)
    return df


# ---------------------------------------------------------------------------
# Agregação / KPIs
# ---------------------------------------------------------------------------

def ordenar_ranking(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(by="valor", ascending=False).reset_index(drop=True)


def calcular_kpis(df_ordenado: pd.DataFrame) -> dict:
    if df_ordenado.empty:
        return {"maior": None, "menor": None, "media": None}

    maior = df_ordenado.iloc[0]
    menor = df_ordenado.iloc[-1]

    return {
        "maior": {"nome": maior["nome"], "valor": float(maior["valor"])},
        "menor": {"nome": menor["nome"], "valor": float(menor["valor"])},
        "media": float(df_ordenado["valor"].mean()),
    }


def calcular_kpi_total(df: pd.DataFrame) -> int:
    return int(len(df))


def filtrar_por_regiao(df: pd.DataFrame, regiao: str) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    if not regiao or regiao.strip().lower() == "brasil":
        return df.copy()

    mapa_regioes = {
        "norte": "N",
        "nordeste": "NE",
        "sudeste": "SE",
        "sul": "S",
        "centro-oeste": "CO",
        "centro oeste": "CO",
    }

    regiao_limpa = regiao.strip().lower()
    regiao_sigla = mapa_regioes.get(regiao_limpa, regiao.strip().upper())

    return df[df["regiao"].str.upper() == regiao_sigla].reset_index(drop=True)


def formatar_numero(valor: float, modo: str = "auto", sufixo: str = "") -> str:
    valor_abs = abs(valor)

    if modo == "decimal":
        texto = f"{valor:.2f}"
    else:
        if valor_abs >= 1_000_000:
            texto = f"{valor / 1_000_000:.1f} milhões"
        elif valor_abs >= 1_000:
            texto = f"{valor / 1_000:.1f} mil"
        else:
            texto = f"{valor:.2f}"

    texto = texto.replace(".", ",")

    if sufixo:
        texto = f"{texto} {sufixo}"

    return texto


def gerar_frase_insight(nome_indicador: str, regiao: str, kpis: dict, modo: str = "auto", sufixo: str = "") -> str:
    if kpis["maior"] is None:
        return f"Não há dados de {nome_indicador} para '{regiao}'."

    recorte = "todo o Brasil" if regiao.strip().lower() == "brasil" else regiao

    return (
        f"Em {recorte}, {kpis['maior']['nome']} lidera em {nome_indicador} "
        f"({formatar_numero(kpis['maior']['valor'], modo, sufixo)}),\n enquanto "
        f"{kpis['menor']['nome']} fica na última posição "
        f"({formatar_numero(kpis['menor']['valor'], modo, sufixo)}). \n"
        f"A média do recorte é {formatar_numero(kpis['media'], modo, sufixo)}."
    )


def montar_resultado(df: pd.DataFrame, nome_indicador: str, regiao: str, modo: str = "auto", sufixo: str = "") -> dict:
    df_regiao = filtrar_por_regiao(df, regiao)
    df_ordenado = ordenar_ranking(df_regiao)
    kpis = calcular_kpis(df_ordenado)
    kpi_total = calcular_kpi_total(df_ordenado)
    insight = gerar_frase_insight(nome_indicador, regiao, kpis, modo, sufixo)

    return {
        "indicador": nome_indicador,
        "regiao": regiao,
        "total_estado": kpi_total,
        "kpis": kpis,
        "insight": insight,
        "ranking": df_ordenado.to_dict(orient="records"),
    }


# ---------------------------------------------------------------------------
# Figura
# ---------------------------------------------------------------------------

def montar_figura(df_ordenado: pd.DataFrame, nome_indicador: str, regiao: str, sufixo: str = "", insight: str = None) -> dict:
    recorte = "Brasil" if not regiao or regiao.strip().lower() == "brasil" else regiao
    label_valor = f"{nome_indicador} ({sufixo})" if sufixo else nome_indicador

    if df_ordenado.empty:
        fig = px.bar(title=f"{nome_indicador} — {recorte} (sem dados no recorte)")
        return json.loads(fig.to_json())

    fig = px.bar(
        df_ordenado,
        x="valor",
        y="nome",
        orientation="h",
        labels={"valor": label_valor, "nome": ""},
        title=f"{nome_indicador} por estado — {recorte} ({len(df_ordenado)} estados)",
    )

    fig.update_yaxes(autorange="reversed")

    margem_inferior = 10
    if insight:
        texto_insight = insight.replace("\n", "<br>")
        fig.add_annotation(
            text=texto_insight,
            xref="paper", yref="paper",
            x=0, y=0,
            xanchor="left", yanchor="top",
            yshift=-45,
            showarrow=False,
            align="left",
            bgcolor="rgba(255,255,255,0.92)",
            bordercolor="#dddddd",
            borderwidth=1,
            borderpad=8,
            font=dict(size=12, color="#333333"),
        )
        margem_inferior = 110

    fig.update_layout(margin=dict(l=10, r=10, t=60, b=margem_inferior))

    return json.loads(fig.to_json())


# ---------------------------------------------------------------------------
# Modelos / Enums da API
# ---------------------------------------------------------------------------

class Indicador(str, Enum):
    area = "area"
    densidade = "densidade"
    populacao = "populacao"


class Regiao(str, Enum):
    brasil = "brasil"
    norte = "norte"
    nordeste = "nordeste"
    sudeste = "sudeste"
    sul = "sul"
    centro_oeste = "centro-oeste"


class KPIItem(BaseModel):
    nome: str
    valor: float


class KPIs(BaseModel):
    total: int
    menor: KPIItem
    maior: KPIItem
    media: float


class GraficoResponse(BaseModel):
    indicador: str
    regiao: str
    figura: dict[str, Any]
    kpis: KPIs


# ---------------------------------------------------------------------------
# FastAPI — dados carregados no startup (não na importação do módulo)
# ---------------------------------------------------------------------------

DFs: dict[str, pd.DataFrame] = {}


def carregar_dados():
    """Busca estados + indicadores no IBGE (ou cache no Postgres) e monta os DataFrames."""
    init_cache_table()

    todos_estados = buscar_estados()
    todos_ids = [int(estado["id"]) for estado in todos_estados]
    print(f"Total de estados: {len(todos_ids)}")

    print("Buscando indicadores...")
    populacao_raw = buscar_indicador(6579, todos_ids)
    densidade_raw = buscar_indicador(1298, todos_ids)
    area_raw = buscar_indicador(1301, todos_ids)

    df_populacao = mesclar_regiao(indicadores_para_df(populacao_raw), todos_estados)
    df_densidade = mesclar_regiao(indicadores_para_df(densidade_raw), todos_estados)
    df_area = mesclar_regiao(indicadores_para_df(area_raw), todos_estados)

    DFs["populacao"] = df_populacao
    DFs["densidade"] = df_densidade
    DFs["area"] = df_area

    print("Dados carregados com sucesso.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    carregar_dados()
    yield
    # Shutdown (nada a fazer por enquanto)


app = FastAPI(title="Brasil em Números", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/grafico", response_model=GraficoResponse)
def grafico(indicador: Indicador, regiao: Regiao = Regiao.brasil):
    if not DFs:
        raise HTTPException(status_code=503, detail="Dados ainda não carregados, tente novamente em instantes.")

    df = DFs[indicador]

    df_filtrado = filtrar_por_regiao(df, regiao)
    df_ordenado = ordenar_ranking(df_filtrado)

    total = calcular_kpi_total(df_ordenado)
    kpis = calcular_kpis(df_ordenado)
    insight = gerar_frase_insight(indicador.value, regiao.value, kpis)
    figura = montar_figura(df_ordenado, indicador.value, regiao.value, insight=insight)

    return {
        "indicador": indicador,
        "regiao": regiao,
        "figura": figura,
        "kpis": {
            "total": total,
            "menor": kpis["menor"],
            "maior": kpis["maior"],
            "media": kpis["media"],
        },
    }