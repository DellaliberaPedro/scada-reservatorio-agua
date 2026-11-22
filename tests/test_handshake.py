"""Handshake e negociação de baud: parsers do protocolo e máquina de estados (tudo puro, sem porta nem Qt)."""
import pytest
from models.handshake import (
    EstadoHandshake, Handshake, Passo, TENTATIVAS_HELLO, TENTATIVAS_PASSO,
)
from models.serial_protocol import (
    BAUD_INICIAL, BAUDS_SUPORTADOS, LinhaInvalida, MensagemHandshake, TipoHandshake,
    eh_linha_de_confirmacao, eh_linha_de_evento, eh_linha_de_handshake, eh_linha_de_leitura,
    escolher_baud, interpretar_handshake, montar_comando_baud,
)

READY = "H,READY,5.0,9600;19200;38400;115200"


# ---------------- parsers ----------------
def test_baud_inicial_e_a_lista_do_enunciado():
    assert BAUD_INICIAL == 9600
    assert BAUDS_SUPORTADOS == (9600, 19200, 38400, 115200)


def test_mensagens_validas():
    assert interpretar_handshake(READY) == MensagemHandshake(
        TipoHandshake.READY, firmware="5.0", bauds=(9600, 19200, 38400, 115200))
    assert interpretar_handshake("H,READY,5.1,9600\r\n").bauds == (9600,)
    assert interpretar_handshake("H,BAUD,115200") == MensagemHandshake(TipoHandshake.BAUD, baud=115200)
    assert interpretar_handshake("H,START\n") == MensagemHandshake(TipoHandshake.START)
    assert interpretar_handshake("H,ERRO,BAUD") == MensagemHandshake(TipoHandshake.ERRO, motivo="BAUD")
    assert interpretar_handshake("H,ERRO,ESTADO").motivo == "ESTADO"


@pytest.mark.parametrize("linha", [
    "H,", "H,READY", "H,READY,5.0", "H,READY,,9600", "H,READY,5.0,", "H,READY,5.0,abc", "H,READY,5.0,9600;",
    "H,READY,5.0,9600;abc", "H,READY,5.0,0", "H,READY,5.0,-1", "H,READY,5.0,96.0", "H,READY,5.0,9600,1",
    "H,READY,5.0,9 600", "H,READY,5.0,+9600", "H,READY,5.0,1e4", "H,READY,5.0,\u0669\u0666\u0660\u0660",
    "H,BAUD", "H,BAUD,", "H,BAUD,abc", "H,BAUD,0", "H,BAUD,-1", "H,BAUD,96.0", "H,BAUD,9600,1", "H,BAUD,\u0669\u0666\u0660\u0660",
    "H,START,1", "H,ERRO", "H,ERRO,", "H,ERRO,A,B",
    "H,XYZ", "H,ready,5.0,9600", "h,START", "START", "BAUD,9600", "HELLO", "L,1,2,3", "A,RELE_ON", "E,BOTAO", "",
])
def test_mensagens_invalidas(linha):
    with pytest.raises(LinhaInvalida):
        interpretar_handshake(linha)


def test_prefixos_nao_se_confundem():
    assert eh_linha_de_handshake("H,START")
    assert not any(f("H,START") for f in (eh_linha_de_leitura, eh_linha_de_confirmacao, eh_linha_de_evento))
    assert not eh_linha_de_handshake("L,1,2,3") and not eh_linha_de_handshake("A,RELE_ON") and not eh_linha_de_handshake("E,BOTAO")


@pytest.mark.parametrize("baud", BAUDS_SUPORTADOS)
def test_montar_comando_baud(baud):
    assert montar_comando_baud(baud) == f"BAUD,{baud}"


@pytest.mark.parametrize("baud", [57600, 4800, 0, -9600, 9600.0, "9600", None, True, 115201])
def test_montar_comando_baud_recusa_o_que_o_scada_nao_fala(baud):
    with pytest.raises(ValueError):
        montar_comando_baud(baud)


@pytest.mark.parametrize("desejado,placa,esperado", [
    (115200, (9600, 19200, 38400, 115200), 115200),   # tudo em comum: o desejado
    (38400, (9600, 19200, 38400, 115200), 38400),
    (9600, (9600, 19200, 38400, 115200), 9600),
    (57600, (9600, 19200, 38400, 115200), 38400),     # fora da lista: o maior comum que não passa
    (115200, (9600, 19200), 19200),                   # a placa é mais lenta: o maior que ela fala
    (115200, (9600,), 9600),
    (115200, (), 9600),                               # nada em comum: 9600 (já está falando assim)
    (1200, (9600, 19200), 9600),                      # abaixo de todos: o menor comum
    (115200, (4800, 57600), 9600),                    # a placa só fala o que o SCADA não fala
    (19200, (115200,), 9600),                         # comum = só 9600 (+ inicial)
])
def test_escolher_baud(desejado, placa, esperado):
    assert escolher_baud(desejado, placa) == esperado


# ---------------- máquina de estados ----------------
def novo(desejado=115200, **k):
    hs = Handshake(desejado, **k)
    assert hs.estado is EstadoHandshake.INICIAL
    return hs


def test_caminho_feliz_9600_para_115200():
    hs = novo(115200)
    assert hs.iniciar() == Passo(enviar="HELLO") and hs.estado is EstadoHandshake.HELLO and hs.baud_atual == 9600

    passo = hs.receber(READY)
    assert passo == Passo(enviar="BAUD,115200") and hs.estado is EstadoHandshake.BAUD
    assert hs.baud_atual == 9600                      # ainda a 9600 até a placa aceitar
    assert hs.firmware == "5.0" and hs.bauds_da_placa == (9600, 19200, 38400, 115200)

    passo = hs.receber("H,BAUD,115200")
    assert passo == Passo(trocar_baud=115200, enviar="START") and hs.estado is EstadoHandshake.START
    assert hs.baud_atual == 115200

    assert hs.receber("H,START") == Passo(concluido=True)
    assert hs.estado is EstadoHandshake.CONCLUIDO and hs.terminou
    assert hs.baud_negociado == 115200 and not hs.fallback


def test_desejado_9600_nao_troca_nada():
    hs = novo(9600); hs.iniciar()
    assert hs.receber(READY) == Passo(enviar="START")           # sem BAUD,<n>
    assert hs.receber("H,START") == Passo(concluido=True) and hs.baud_negociado == 9600


def test_baud_fora_da_lista_do_scada_vira_o_maior_comum_com_aviso():
    hs = novo(57600); hs.iniciar()
    passo = hs.receber(READY)
    assert passo.enviar == "BAUD,38400"
    assert "57600" in passo.aviso and "38400" in passo.aviso


def test_placa_mais_lenta_que_o_desejado():
    hs = novo(115200); hs.iniciar()
    passo = hs.receber("H,READY,4.0,9600;19200")
    assert passo.enviar == "BAUD,19200" and "115200" in passo.aviso


def test_placa_que_so_fala_9600_pula_o_baud_com_aviso():
    hs = novo(115200); hs.iniciar()
    passo = hs.receber("H,READY,4.0,9600")
    assert passo.enviar == "START" and passo.aviso and hs.estado is EstadoHandshake.START


def test_sem_aviso_quando_o_desejado_foi_aceito():
    hs = novo(115200); hs.iniciar()
    assert hs.receber(READY).aviso is None


def test_placa_recusa_o_baud_fallback_a_9600():
    hs = novo(115200); hs.iniciar(); hs.receber(READY)
    passo = hs.receber("H,ERRO,BAUD")
    assert passo.enviar == "START" and passo.trocar_baud is None and "9600" in passo.aviso
    assert hs.fallback and hs.baud_atual == 9600 and hs.estado is EstadoHandshake.START
    assert hs.receber("H,START").concluido and hs.baud_negociado == 9600


def test_linhas_que_nao_importam_sao_ignoradas_em_todos_os_estados():
    ruido = ["L,54.30,147.00,24.10", "A,RELE_OFF", "E,BOTAO", "lixo", "", "H,", "H,XYZ", "\xff", "H,READY"[:-2]]
    hs = novo(); assert hs.receber("H,START") is None                  # antes do iniciar(): ignora
    hs.iniciar()
    for linha in ruido:
        assert hs.receber(linha) is None and hs.estado is EstadoHandshake.HELLO
    hs.receber(READY)
    for linha in ruido + [READY]:                                         # READY repetido (HELLO reenviado)
        assert hs.receber(linha) is None and hs.estado is EstadoHandshake.BAUD
    assert hs.receber("H,BAUD,19200") is None and hs.estado is EstadoHandshake.BAUD   # outro valor: ignora
    assert hs.receber("H,START") is None and hs.estado is EstadoHandshake.BAUD        # START antes da hora
    hs.receber("H,BAUD,115200")
    for linha in ruido + [READY, "H,BAUD,115200"]:
        assert hs.receber(linha) is None and hs.estado is EstadoHandshake.START


def test_erro_no_start_derruba_com_o_motivo():
    hs = novo(); hs.iniciar(); hs.receber(READY); hs.receber("H,BAUD,115200")
    passo = hs.receber("H,ERRO,ESTADO")
    assert passo.falha and "ESTADO" in passo.falha and hs.estado is EstadoHandshake.FALHOU


def test_erro_no_hello_e_ignorado():
    hs = novo(); hs.iniciar()
    assert hs.receber("H,ERRO,ESTADO") is None and hs.estado is EstadoHandshake.HELLO


def test_tempo_do_hello_reenvia_e_depois_desiste():
    hs = novo(); hs.iniciar()
    for _ in range(TENTATIVAS_HELLO - 1):
        assert hs.estourou_tempo() == Passo(enviar="HELLO")
    passo = hs.estourou_tempo()
    assert passo.enviar is None and "HELLO" in passo.falha and "9600" in passo.falha
    assert hs.estado is EstadoHandshake.FALHOU


def test_tolera_pelo_menos_tres_hello_sem_resposta():
    """A placa pode estar no meio de uma leitura (~1,3 s): os 3 primeiros HELLO sem resposta não derrubam."""
    hs = novo(); hs.iniciar()
    assert hs.estourou_tempo() == Passo(enviar="HELLO")          # 2º HELLO
    assert hs.estourou_tempo() == Passo(enviar="HELLO")          # 3º HELLO
    assert hs.estado is EstadoHandshake.HELLO
    assert hs.receber(READY).enviar == "BAUD,115200"             # e a resposta tardia ainda é aceita


def test_tempo_do_baud_reenvia_o_mesmo_baud_e_desiste():
    hs = novo(38400); hs.iniciar(); hs.receber(READY)
    for _ in range(TENTATIVAS_PASSO - 1):
        assert hs.estourou_tempo() == Passo(enviar="BAUD,38400")
    passo = hs.estourou_tempo()
    assert "38400" in passo.falha and hs.estado is EstadoHandshake.FALHOU


def test_tempo_do_start_reenvia_start_e_desiste():
    hs = novo(); hs.iniciar(); hs.receber(READY); hs.receber("H,BAUD,115200")
    for _ in range(TENTATIVAS_PASSO - 1):
        assert hs.estourou_tempo() == Passo(enviar="START")
    assert "START" in hs.estourou_tempo().falha


def test_cada_fase_recebe_todas_as_tentativas_de_novo():
    hs = novo(); hs.iniciar()
    for _ in range(TENTATIVAS_HELLO - 1):
        hs.estourou_tempo()                                   # gasta quase todas as tentativas do HELLO
    hs.receber(READY)
    for _ in range(TENTATIVAS_PASSO - 1):                     # o BAUD tem as suas próprias
        assert hs.estourou_tempo().enviar == "BAUD,115200"
    hs.receber("H,BAUD,115200")
    for _ in range(TENTATIVAS_PASSO - 1):
        assert hs.estourou_tempo().enviar == "START"


def test_depois_de_terminar_nada_mais_acontece():
    hs = novo(); hs.iniciar(); hs.receber(READY); hs.receber("H,BAUD,115200"); hs.receber("H,START")
    assert hs.receber(READY) is None and hs.estourou_tempo() == Passo()
    assert hs.estado is EstadoHandshake.CONCLUIDO
    falho = novo(); falho.iniciar()
    for _ in range(TENTATIVAS_HELLO):
        falho.estourou_tempo()
    assert falho.receber(READY) is None and falho.estourou_tempo() == Passo()
    assert falho.estado is EstadoHandshake.FALHOU


def test_lista_de_bauds_do_pc_pode_ser_trocada():
    hs = Handshake(115200, bauds_do_pc=(9600, 19200)); hs.iniciar()
    assert hs.receber(READY).enviar == "BAUD,19200"
