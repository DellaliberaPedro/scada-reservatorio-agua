import pytest
from models.serial_protocol import (
    EventoPlaca, LinhaInvalida, SensorSemLeitura, TipoEventoPlaca,
    eh_linha_de_confirmacao, eh_linha_de_evento, eh_linha_de_leitura,
    ConfirmacaoLimite, interpretar_confirmacao, interpretar_confirmacao_limite, interpretar_evento,
    interpretar_leitura, montar_comando_limite,
)

def test_confirmacoes_validas():
    assert interpretar_confirmacao("A,RELE_ON") == "RELE_ON"
    assert interpretar_confirmacao("A,RELE_OFF\r\n") == "RELE_OFF"

@pytest.mark.parametrize("linha", ["A,", "A,FOO", "A,rele_on", "A,RELE_ON,1", "RELE_ON", "L,1,2,3"])
def test_confirmacoes_invalidas(linha):
    with pytest.raises(LinhaInvalida):
        interpretar_confirmacao(linha)

def test_eventos_validos():
    assert interpretar_evento("E,LIMITE,TDS") == EventoPlaca(TipoEventoPlaca.LIMITE, "TDS")
    assert interpretar_evento("E,LIMITE,TURBIDEZ").sensor == "TURBIDEZ"
    assert interpretar_evento("E,LIMITE,TEMPERATURA").sensor == "TEMPERATURA"
    assert interpretar_evento("E,BOTAO") == EventoPlaca(TipoEventoPlaca.BOTAO)
    assert interpretar_evento("E,BLOQUEADO\n") == EventoPlaca(TipoEventoPlaca.BLOQUEADO)

@pytest.mark.parametrize("linha", [
    "E,", "E,LIMITE", "E,LIMITE,", "E,LIMITE,PH", "E,LIMITE,TDS,1", "E,BOTAO,1", "E,BLOQUEADO,x",
    "E,XYZ", "E,limite,TDS", "LIMITE,TDS", "A,RELE_ON",
])
def test_eventos_invalidos(linha):
    with pytest.raises(LinhaInvalida):
        interpretar_evento(linha)

def test_prefixos_nao_se_confundem():
    assert eh_linha_de_leitura("L,1,2,3") and not eh_linha_de_evento("L,1,2,3")
    assert eh_linha_de_evento("E,BOTAO") and not eh_linha_de_confirmacao("E,BOTAO")
    assert eh_linha_de_confirmacao("A,RELE_ON") and not eh_linha_de_leitura("A,RELE_ON")

def test_leitura_continua_igual():
    l = interpretar_leitura("L,54.30,147.00,24.10")
    assert (l.tds_ppm, l.turbidez_ntu, l.temperatura_c) == (54.3, 147.0, 24.1)
    with pytest.raises(SensorSemLeitura):
        interpretar_leitura("L,54.30,147.00,-127.00")
    with pytest.raises(LinhaInvalida):
        interpretar_leitura("L,54.30,147.00")


# ---------------- limites ----------------
def test_montar_comando_limite():
    assert montar_comando_limite("TDS", 250) == "LIMITE,TDS,250.00"
    assert montar_comando_limite("TURBIDEZ", 185.5) == "LIMITE,TURBIDEZ,185.50"
    assert montar_comando_limite("TEMPERATURA", 28.456) == "LIMITE,TEMPERATURA,28.46"
    assert montar_comando_limite("TDS", 10000) == "LIMITE,TDS,10000.00"
    assert montar_comando_limite("TDS", 0.01) == "LIMITE,TDS,0.01"

@pytest.mark.parametrize("sensor,valor", [
    ("PH", 10), ("tds", 10), ("", 10), ("TDS", 0), ("TDS", -1), ("TDS", 10000.01), ("TDS", float("nan")),
    ("TDS", float("inf")), ("TDS,TURBIDEZ", 5),
])
def test_montar_comando_limite_recusa_o_que_a_placa_recusaria(sensor, valor):
    with pytest.raises(ValueError):
        montar_comando_limite(sensor, valor)

def test_confirmacao_de_limite_valida():
    assert interpretar_confirmacao_limite("A,LIMITE,TDS,250.00") == ConfirmacaoLimite("TDS", 250.0)
    assert interpretar_confirmacao_limite("A,LIMITE,TEMPERATURA,28.50\r\n") == ConfirmacaoLimite("TEMPERATURA", 28.5)

@pytest.mark.parametrize("linha", [
    "A,LIMITE", "A,LIMITE,TDS", "A,LIMITE,TDS,", "A,LIMITE,TDS,abc", "A,LIMITE,PH,10", "A,LIMITE,TDS,0",
    "A,LIMITE,TDS,-5", "A,LIMITE,TDS,nan", "A,LIMITE,TDS,250,1", "A,RELE_ON", "E,LIMITE,TDS", "LIMITE,TDS,250",
])
def test_confirmacao_de_limite_invalida(linha):
    with pytest.raises(LinhaInvalida):
        interpretar_confirmacao_limite(linha)

def test_confirmacao_de_rele_e_de_limite_nao_se_confundem():
    with pytest.raises(LinhaInvalida):
        interpretar_confirmacao("A,LIMITE,TDS,250.00")      # não é RELE_*
    with pytest.raises(LinhaInvalida):
        interpretar_confirmacao_limite("A,RELE_OFF")
