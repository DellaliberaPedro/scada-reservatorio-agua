"""Série temporal (tabela leituras): gravação, esquema e consulta por período com agrupamento. SQLite real (temporário)."""
import random
import sqlite3
from datetime import datetime, timedelta

import pytest

from database.connection import Database
from models.leitura_repository import LeituraRepository
from models.sensor_data import MAX_PONTOS_GRAFICO, LeituraTelemetria

T0 = datetime(2026, 11, 20, 10, 0, 0)


@pytest.fixture(autouse=True)
def banco_temporario(tmp_path, monkeypatch):
    monkeypatch.setattr(Database, "DB_PATH", tmp_path / "t.db")
    Database.init_db()


def leitura(t, temp=25.0, tds=200.0, turb=5.0, iqa=80.0):
    return LeituraTelemetria(timestamp=t, temperatura_c=temp, tds_ppm=tds, turbidez_ntu=turb, iqa=iqa)


def grava_muitas(leituras):
    """Insere direto (rápido) para montar milhares de linhas sem abrir uma transação por leitura."""
    with Database.transacao() as conn:
        conn.executemany(
            "INSERT INTO leituras (data_hora, temperatura_c, tds_ppm, turbidez_ntu, iqa) VALUES (?, ?, ?, ?, ?)",
            [(l.timestamp.strftime(Database.FORMATO_DATA_HORA), l.temperatura_c, l.tds_ppm, l.turbidez_ntu, l.iqa)
             for l in leituras],
        )


# ---------------- esquema ----------------
def test_tabela_leituras_e_o_indice_por_data_existem():
    with Database.transacao() as conn:
        tabelas = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indices = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "leituras" in tabelas and "idx_leituras_data_hora" in indices


def test_banco_antigo_sem_a_tabela_ganha_a_tabela_ao_iniciar(tmp_path, monkeypatch):
    """O smartgrid.db que o grupo já tem (criado antes desta etapa) não precisa ser apagado."""
    antigo = tmp_path / "antigo.db"
    conn = sqlite3.connect(antigo)
    conn.executescript("CREATE TABLE dispositivos (id INTEGER PRIMARY KEY AUTOINCREMENT, tag TEXT NOT NULL UNIQUE, "
                       "tipo TEXT NOT NULL, descricao TEXT, status TEXT NOT NULL);")
    conn.commit(); conn.close()
    monkeypatch.setattr(Database, "DB_PATH", antigo)
    Database.init_db()
    assert LeituraRepository.contar() == 0


def test_a_validacao_do_esquema_exige_a_tabela_leituras():
    with Database.transacao() as conn:
        conn.execute("DROP TABLE leituras")
        with pytest.raises(RuntimeError, match="leituras"):
            Database._validar_esquema(conn)


def test_consulta_usa_o_indice_de_data_hora():
    with Database.transacao() as conn:
        plano = " ".join(
            r["detail"] for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM leituras WHERE data_hora BETWEEN ? AND ?",
                ("2026-11-20 00:00:00", "2026-11-21 00:00:00")))
    assert "idx_leituras_data_hora" in plano


# ---------------- inserir ----------------
def test_inserir_grava_todos_os_campos_e_devolve_ids_crescentes():
    a = LeituraRepository.inserir(leitura(T0, temp=23.5, tds=150.25, turb=3.5, iqa=91.0))
    b = LeituraRepository.inserir(leitura(T0 + timedelta(seconds=2)))
    assert (a, b) == (1, 2) and LeituraRepository.contar() == 2
    with Database.transacao() as conn:
        linha = conn.execute("SELECT * FROM leituras WHERE id = 1").fetchone()
    assert (linha["data_hora"], linha["temperatura_c"], linha["tds_ppm"], linha["turbidez_ntu"], linha["iqa"]) == \
           ("2026-11-20 10:00:00", 23.5, 150.25, 3.5, 91.0)


def test_inserir_com_conexao_participa_da_transacao_de_quem_chamou():
    with pytest.raises(RuntimeError):
        with Database.transacao() as conn:
            LeituraRepository.inserir(leitura(T0), conn)
            raise RuntimeError("falha depois da leitura")
    assert LeituraRepository.contar() == 0                      # rollback desfez a leitura também


@pytest.mark.parametrize("campo", ["tds", "turb"])
def test_valor_negativo_e_recusado_pelo_banco(campo):
    with pytest.raises(sqlite3.IntegrityError):
        LeituraRepository.inserir(leitura(T0, **{campo: -1.0}))
    assert LeituraRepository.contar() == 0


def test_temperatura_negativa_e_valida():
    LeituraRepository.inserir(leitura(T0, temp=-5.0))              # DS18B20 mede até -55 °C
    assert LeituraRepository.contar() == 1


# ---------------- consultar_periodo: validação ----------------
@pytest.mark.parametrize("inicio, fim", [(T0, T0), (T0 + timedelta(seconds=1), T0)])
def test_periodo_invalido_levanta_valueerror(inicio, fim):
    with pytest.raises(ValueError):
        LeituraRepository.consultar_periodo(inicio, fim)


@pytest.mark.parametrize("n", [0, -3])
def test_max_pontos_invalido_levanta_valueerror(n):
    with pytest.raises(ValueError):
        LeituraRepository.consultar_periodo(T0, T0 + timedelta(hours=1), n)


def test_periodo_sem_leituras_devolve_total_zero_e_sem_estatistica():
    LeituraRepository.inserir(leitura(T0 - timedelta(days=3)))
    c = LeituraRepository.consultar_periodo(T0, T0 + timedelta(hours=1))
    assert c.total_leituras == 0 and c.pontos == () and c.temperatura is None and c.iqa is None
    assert (c.inicio, c.fim) == (T0, T0 + timedelta(hours=1))


def test_o_padrao_de_pontos_e_o_do_grafico():
    assert MAX_PONTOS_GRAFICO == 1200


# ---------------- consultar_periodo: dados ----------------
def test_pontas_do_periodo_valem_e_o_que_esta_fora_nao_entra():
    fim = T0 + timedelta(minutes=10)
    for t, temp in [(T0 - timedelta(seconds=1), 1.0), (T0, 2.0), (T0 + timedelta(minutes=5), 3.0),
                    (fim, 4.0), (fim + timedelta(seconds=1), 5.0)]:
        LeituraRepository.inserir(leitura(t, temp=temp))
    c = LeituraRepository.consultar_periodo(T0, fim)
    assert c.total_leituras == 3
    assert [p.temperatura_c for p in c.pontos] == [2.0, 3.0, 4.0]


def test_poucas_leituras_saem_uma_por_ponto_em_ordem_cronologica():
    # inseridas fora de ordem de propósito
    for seg in (30, 0, 20, 10):
        LeituraRepository.inserir(leitura(T0 + timedelta(seconds=seg), temp=float(seg)))
    c = LeituraRepository.consultar_periodo(T0, T0 + timedelta(seconds=40))
    assert c.segundos_por_ponto == 1
    assert [p.timestamp for p in c.pontos] == [T0 + timedelta(seconds=s) for s in (0, 10, 20, 30)]
    assert [p.temperatura_c for p in c.pontos] == [0.0, 10.0, 20.0, 30.0]
    assert all(p.amostras == 1 for p in c.pontos)


def test_estatistica_de_pouca_leitura_e_exata():
    for seg, (temp, tds, turb, iqa) in enumerate([(20, 100, 1, 90), (30, 300, 9, 70), (25, 200, 5, 80)]):
        LeituraRepository.inserir(leitura(T0 + timedelta(seconds=seg), temp, tds, turb, iqa))
    c = LeituraRepository.consultar_periodo(T0, T0 + timedelta(minutes=1))
    assert (c.temperatura.minimo, c.temperatura.media, c.temperatura.maximo) == (20, 25, 30)
    assert (c.tds.minimo, c.tds.media, c.tds.maximo) == (100, 200, 300)
    assert (c.turbidez.minimo, c.turbidez.media, c.turbidez.maximo) == (1, 5, 9)
    assert (c.iqa.minimo, c.iqa.media, c.iqa.maximo) == (70, 80, 90)


def test_muitas_leituras_viram_no_maximo_max_pontos_pontos():
    grava_muitas([leitura(T0 + timedelta(seconds=s), temp=20 + (s % 10)) for s in range(3600)])
    c = LeituraRepository.consultar_periodo(T0, T0 + timedelta(seconds=3599), max_pontos=60)
    assert c.total_leituras == 3600
    assert len(c.pontos) <= 60 and c.segundos_por_ponto > 1
    assert sum(p.amostras for p in c.pontos) == 3600               # nenhuma leitura se perde no agrupamento


@pytest.mark.parametrize("max_pontos", [1, 2, 7, 60, 100])
@pytest.mark.parametrize("deslocamento_s", [0, 1, 17, 59])
def test_nunca_passa_de_max_pontos_nem_com_o_inicio_desalinhado(max_pontos, deslocamento_s):
    """Intervalos contados a partir do início do período: leitura em `fim` e início 'quebrado' não geram ponto a mais."""
    inicio = T0 + timedelta(seconds=deslocamento_s)
    fim = inicio + timedelta(seconds=3600)
    grava_muitas([leitura(inicio + timedelta(seconds=s)) for s in range(0, 3601)])   # inclui as duas pontas
    c = LeituraRepository.consultar_periodo(inicio, fim, max_pontos)
    assert c.total_leituras == 3601
    assert 1 <= len(c.pontos) <= max_pontos


def test_estatistica_do_agrupamento_bate_com_a_conta_feita_nas_leituras_cruas():
    rnd = random.Random(7)
    cruas = [leitura(T0 + timedelta(seconds=s), temp=rnd.uniform(10, 40), tds=rnd.uniform(0, 600),
                     turb=rnd.uniform(0, 200), iqa=rnd.uniform(0, 100))
             for s in range(0, 5000, 2)]
    # buracos: intervalos com quantidades de leituras MUITO diferentes (a média simples das médias erraria)
    cruas = [l for i, l in enumerate(cruas) if not (400 <= i < 900)]
    grava_muitas(cruas)
    c = LeituraRepository.consultar_periodo(T0, T0 + timedelta(seconds=5000), max_pontos=37)
    assert c.total_leituras == len(cruas) and len(c.pontos) <= 37 and c.segundos_por_ponto > 1
    for sensor, attr in (("temperatura", "temperatura_c"), ("tds", "tds_ppm"),
                         ("turbidez", "turbidez_ntu"), ("iqa", "iqa")):
        valores = [getattr(l, attr) for l in cruas]
        est = getattr(c, sensor)
        assert est.minimo == pytest.approx(min(valores))
        assert est.maximo == pytest.approx(max(valores))
        assert est.media == pytest.approx(sum(valores) / len(valores))


def test_media_ponderada_e_nao_a_media_das_medias():
    # período de 200 s em no máx. 2 pontos -> intervalos de 101 s.
    # intervalo A: 1 leitura de 10; intervalo B: 3 leituras de 20 -> média do período = 17,5 (a das médias seria 15)
    LeituraRepository.inserir(leitura(T0, temp=10.0))
    for s in (101, 102, 103):
        LeituraRepository.inserir(leitura(T0 + timedelta(seconds=s), temp=20.0))
    c = LeituraRepository.consultar_periodo(T0, T0 + timedelta(seconds=200), max_pontos=2)
    assert len(c.pontos) == 2 and [p.amostras for p in c.pontos] == [1, 3]
    assert c.temperatura.media == pytest.approx(17.5)


def test_pontos_ficam_em_ordem_e_cada_um_traz_o_horario_da_primeira_leitura_do_intervalo():
    grava_muitas([leitura(T0 + timedelta(seconds=s), temp=float(s)) for s in range(0, 1000)])
    c = LeituraRepository.consultar_periodo(T0, T0 + timedelta(seconds=999), max_pontos=10)
    tempos = [p.timestamp for p in c.pontos]
    assert tempos == sorted(tempos) and tempos[0] == T0
    seg = c.segundos_por_ponto
    assert [(t - T0).total_seconds() for t in tempos] == [i * seg for i in range(len(tempos))]
    assert c.pontos[0].temperatura_c == pytest.approx(sum(range(seg)) / seg)       # média do 1º intervalo


def test_leitura_de_outro_dia_nao_entra_no_periodo_de_hoje():
    LeituraRepository.inserir(leitura(T0 - timedelta(days=1), temp=99.0))
    LeituraRepository.inserir(leitura(T0, temp=20.0))
    c = LeituraRepository.consultar_periodo(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert c.total_leituras == 1 and c.temperatura.maximo == 20.0


def test_cinquenta_mil_leituras_em_sete_dias_viram_ate_1200_pontos_com_todas_contadas():
    inicio = T0
    fim = T0 + timedelta(days=7)
    # 50 mil leituras espalhadas pelos 7 dias (uma a cada ~12 s)
    grava_muitas([leitura(inicio + timedelta(seconds=12 * i), temp=20 + (i % 7)) for i in range(50_000)])
    c = LeituraRepository.consultar_periodo(inicio, fim)
    assert c.total_leituras == 50_000
    assert len(c.pontos) <= MAX_PONTOS_GRAFICO
    assert sum(p.amostras for p in c.pontos) == 50_000
    assert c.temperatura.minimo == 20 and c.temperatura.maximo == 26


def test_microssegundos_nas_pontas_nao_quebram_a_consulta():
    LeituraRepository.inserir(leitura(T0 + timedelta(seconds=1)))
    c = LeituraRepository.consultar_periodo(T0 + timedelta(microseconds=300), T0 + timedelta(seconds=2, microseconds=900))
    assert c.total_leituras == 1
