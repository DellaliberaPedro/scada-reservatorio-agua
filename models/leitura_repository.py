"""
Repositório da série temporal dos sensores (tabela leituras).

Todos os comandos usam placeholders '?'. Em runtime este repositório só é chamado pela THREAD DO BANCO
(models/database_worker.py): a tela pede, a thread consulta, o resultado volta por sinal.

Gráfico com muitos pontos: 7 dias de leituras a cada 2 s são ~300 mil linhas, pesado demais para desenhar.
Por isso a consulta AGRUPA no próprio SQLite: o período é dividido, a partir do início, em intervalos
iguais (no máximo `max_pontos` deles) e cada intervalo vira UM ponto com a média das leituras dele.
Mínimo, média e máximo do período inteiro continuam exatos (são recalculados a partir dos intervalos,
sem perder nenhuma leitura).
"""
from datetime import datetime, timedelta

from database.connection import Database
from models.sensor_data import (
    MAX_PONTOS_GRAFICO,
    ConsultaLeituras,
    EstatisticaSensor,
    LeituraTelemetria,
    PontoHistorico,
)


class LeituraRepository:

    @staticmethod
    def inserir(leitura: LeituraTelemetria, conn=None) -> int:
        """Grava uma leitura e devolve o id. Com `conn`, participa da transação de quem chamou."""
        sql = (
            "INSERT INTO leituras (data_hora, temperatura_c, tds_ppm, turbidez_ntu, iqa) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        parametros = (
            leitura.timestamp.strftime(Database.FORMATO_DATA_HORA),
            leitura.temperatura_c,
            leitura.tds_ppm,
            leitura.turbidez_ntu,
            leitura.iqa,
        )
        if conn is not None:
            return conn.execute(sql, parametros).lastrowid
        with Database.transacao() as nova_conn:
            return nova_conn.execute(sql, parametros).lastrowid

    @staticmethod
    def contar() -> int:
        with Database.transacao() as conn:
            return conn.execute("SELECT COUNT(*) FROM leituras").fetchone()[0]

    @staticmethod
    def consultar_periodo(inicio: datetime, fim: datetime, max_pontos: int = MAX_PONTOS_GRAFICO) -> ConsultaLeituras:
        """
        Leituras de [inicio, fim] (as duas pontas valem), agrupadas em no máximo `max_pontos` intervalos.
        Levanta ValueError se o período for inválido. Período sem leitura devolve total_leituras = 0.
        """
        if inicio >= fim:
            raise ValueError("o início do período precisa ser anterior ao fim")
        if max_pontos < 1:
            raise ValueError("max_pontos precisa ser pelo menos 1")

        inicio_txt = inicio.strftime(Database.FORMATO_DATA_HORA)
        fim_txt = fim.strftime(Database.FORMATO_DATA_HORA)
        span_s = int((datetime.strptime(fim_txt, Database.FORMATO_DATA_HORA)
                      - datetime.strptime(inicio_txt, Database.FORMATO_DATA_HORA)).total_seconds())
        # tamanho do intervalo: span // max_pontos + 1 garante span / segundos < max_pontos, e como os
        # intervalos são contados a partir do início (leitura em `fim` inclusive) nunca passa de max_pontos
        segundos = span_s // max_pontos + 1
        with Database.transacao() as conn:  # UMA consulta só: pontos e estatística nunca discordam
            linhas = conn.execute(
                "SELECT MIN(data_hora) AS primeira, COUNT(*) AS n, "
                "       AVG(temperatura_c) AS t_med, MIN(temperatura_c) AS t_min, MAX(temperatura_c) AS t_max, "
                "       AVG(tds_ppm)       AS d_med, MIN(tds_ppm)       AS d_min, MAX(tds_ppm)       AS d_max, "
                "       AVG(turbidez_ntu)  AS u_med, MIN(turbidez_ntu)  AS u_min, MAX(turbidez_ntu)  AS u_max, "
                "       AVG(iqa)           AS i_med, MIN(iqa)           AS i_min, MAX(iqa)           AS i_max "
                "FROM leituras WHERE data_hora BETWEEN ? AND ? "
                "GROUP BY (CAST(strftime('%s', data_hora) AS INTEGER) - CAST(strftime('%s', ?) AS INTEGER)) / ? "
                "ORDER BY primeira ASC",
                (inicio_txt, fim_txt, inicio_txt, segundos),
            ).fetchall()

        pontos = tuple(
            PontoHistorico(
                timestamp=datetime.strptime(l["primeira"], Database.FORMATO_DATA_HORA),
                temperatura_c=l["t_med"], tds_ppm=l["d_med"], turbidez_ntu=l["u_med"], iqa=l["i_med"],
                amostras=l["n"],
            )
            for l in linhas
        )
        total = sum(l["n"] for l in linhas)
        if total == 0:
            return ConsultaLeituras(inicio, fim, 0, segundos)

        def estatistica(media, minimo, maximo):
            # média das médias PONDERADA pelo número de leituras de cada intervalo = média exata do período
            return EstatisticaSensor(
                minimo=min(l[minimo] for l in linhas),
                media=sum(l[media] * l["n"] for l in linhas) / total,
                maximo=max(l[maximo] for l in linhas),
            )

        return ConsultaLeituras(
            inicio=inicio, fim=fim, total_leituras=total, segundos_por_ponto=segundos, pontos=pontos,
            temperatura=estatistica("t_med", "t_min", "t_max"),
            tds=estatistica("d_med", "d_min", "d_max"),
            turbidez=estatistica("u_med", "u_min", "u_max"),
            iqa=estatistica("i_med", "i_min", "i_max"),
        )
