"""Carga dos 5.570 municípios do IBGE (lista + malha territorial) na tabela municipio.

  lista: https://servicodados.ibge.gov.br/api/v1/localidades/municipios?view=nivelado
  malha: https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR?intrarregiao=municipio (GeoJSON, ~12 MB)

Idempotente: reaplicar só atualiza nome/geometria. Municípios novos sem malha ficam com geom NULL.
"""
from __future__ import annotations

import json
import logging
import unicodedata

import psycopg

from .config import settings
from .http import get_json

log = logging.getLogger(__name__)

LIST_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios?view=nivelado"
MESH_URL = ("https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR"
            "?formato=application/vnd.geo+json&intrarregiao=municipio&qualidade={q}")


def slug(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower().strip()


def is_loaded(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT count(*) AS n FROM municipio WHERE geom IS NOT NULL").fetchone()["n"] > 5000


def load_all(conn: psycopg.Connection) -> dict[str, int]:
    log.info("ibge: baixando lista de municípios")
    lista = get_json(LIST_URL)
    ufs = {m["UF-id"]: (m["UF-sigla"], m["UF-nome"], m["regiao-nome"]) for m in lista}
    with conn.transaction():
        for uid, (sigla, nome, regiao) in sorted(ufs.items()):
            conn.execute(
                "INSERT INTO uf (id, sigla, nome, regiao) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET sigla = EXCLUDED.sigla, nome = EXCLUDED.nome, regiao = EXCLUDED.regiao",
                (uid, sigla, nome, regiao),
            )
        for m in lista:
            conn.execute(
                "INSERT INTO municipio (id, uf_id, nome, nome_busca) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET uf_id = EXCLUDED.uf_id, nome = EXCLUDED.nome, nome_busca = EXCLUDED.nome_busca",
                (m["municipio-id"], m["UF-id"], m["municipio-nome"], slug(m["municipio-nome"])),
            )
    log.info("ibge: %d UFs, %d municípios; baixando malha (%s)", len(ufs), len(lista), settings.ibge_mesh_quality)

    mesh = get_json(MESH_URL.format(q=settings.ibge_mesh_quality))
    known = {m["municipio-id"] for m in lista}
    loaded = skipped = 0
    with conn.transaction():
        for f in mesh.get("features") or []:
            mid = int(f["properties"]["codarea"])
            if mid not in known:
                skipped += 1
                continue
            conn.execute(
                "UPDATE municipio SET geom = ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)) WHERE id = %s",
                (json.dumps(f["geometry"]), mid),
            )
            loaded += 1
        # centro + raio do círculo que envolve a caixa do polígono (para a busca por raio das fontes)
        conn.execute(
            """
            UPDATE municipio SET
                centroid_lat = ST_Y(ST_Centroid(geom)),
                centroid_lon = ST_X(ST_Centroid(geom)),
                radius_m = ceil(greatest(
                    ST_Distance(ST_Centroid(geom)::geography, ST_PointN(ST_ExteriorRing(ST_Envelope(geom)), 1)::geography),
                    ST_Distance(ST_Centroid(geom)::geography, ST_PointN(ST_ExteriorRing(ST_Envelope(geom)), 2)::geography),
                    ST_Distance(ST_Centroid(geom)::geography, ST_PointN(ST_ExteriorRing(ST_Envelope(geom)), 3)::geography),
                    ST_Distance(ST_Centroid(geom)::geography, ST_PointN(ST_ExteriorRing(ST_Envelope(geom)), 4)::geography)))
            WHERE geom IS NOT NULL
            """
        )
        # estações antigas (coletadas por raio) ganham município pelo ponto
        n = conn.execute(
            "UPDATE station SET municipio_id = municipio_at(lat, lon) "
            "WHERE municipio_id IS NULL AND lat IS NOT NULL AND lon IS NOT NULL"
        ).rowcount
    missing = conn.execute("SELECT count(*) AS n FROM municipio WHERE geom IS NULL").fetchone()["n"]
    log.info("ibge: %d malhas carregadas, %d ignoradas, %d municípios sem malha; %d estações vinculadas", loaded, skipped, missing, n)
    return {"ufs": len(ufs), "municipios": len(lista), "malhas": loaded, "sem_malha": missing, "stations_linked": n}
