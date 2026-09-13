"""Município como unidade de coleta: centro + raio para a busca das fontes, polígono (PostGIS) para o corte."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, TypeVar

import psycopg

T = TypeVar("T")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Municipio:
    conn: psycopg.Connection
    id: int
    nome: str
    uf: str
    centroid_lat: float
    centroid_lon: float
    radius_m: int

    @property
    def label(self) -> str:
        return f"{self.nome}/{self.uf}"

    def search_radius_m(self, margin_m: int = 2000) -> int:
        """Raio para pedir às fontes: cobre o município inteiro com uma folga."""
        return self.radius_m + margin_m

    def near(self, lat: float | None, lon: float | None) -> bool:
        """Pré-filtro barato (círculo envolvente) antes de consultar o polígono."""
        if lat is None or lon is None:
            return False
        return haversine_m(self.centroid_lat, self.centroid_lon, lat, lon) <= self.search_radius_m()

    def contains_many(self, points: list[tuple[float | None, float | None]]) -> list[bool]:
        """Ponto-em-polígono em lote (uma query). None => False."""
        idx = [i for i, (la, lo) in enumerate(points) if la is not None and lo is not None]
        out = [False] * len(points)
        if not idx:
            return out
        rows = self.conn.execute(
            """
            SELECT p.i
              FROM municipio m,
                   unnest(%s::int[], %s::float8[], %s::float8[]) AS p(i, lat, lon)
             WHERE m.id = %s AND ST_Contains(m.geom, ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326))
            """,
            (idx, [points[i][0] for i in idx], [points[i][1] for i in idx], self.id),
        ).fetchall()
        for r in rows:
            out[r["i"]] = True
        return out

    def filter(self, items: Iterable[T], key: Callable[[T], tuple[float | None, float | None]]) -> list[T]:
        """Mantém só os itens cujo (lat, lon) cai dentro do município."""
        items = [it for it in items if self.near(*key(it))]
        inside = self.contains_many([key(it) for it in items])
        return [it for it, ok in zip(items, inside) if ok]


def load(conn: psycopg.Connection, municipio_id: int) -> Municipio:
    row = conn.execute(
        """
        SELECT m.id, m.nome, u.sigla AS uf, m.centroid_lat, m.centroid_lon, m.radius_m, m.geom IS NOT NULL AS has_geom
          FROM municipio m JOIN uf u ON u.id = m.uf_id WHERE m.id = %s
        """,
        (municipio_id,),
    ).fetchone()
    if not row:
        raise LookupError(f"município {municipio_id} não cadastrado (rode load-municipios)")
    if not row["has_geom"]:
        raise LookupError(f"município {row['nome']}/{row['uf']} sem malha IBGE; não dá para coletar")
    return Municipio(conn, row["id"], row["nome"], row["uf"], row["centroid_lat"], row["centroid_lon"], row["radius_m"])
