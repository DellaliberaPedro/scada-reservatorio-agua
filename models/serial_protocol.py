"""
Protocolo serial entre o STM32 e o supervisório (parte pura: sem PySide6 e sem pyserial).

Tudo é texto ASCII terminado em quebra de linha, como no protocolo usado nas aulas de serial:

    STM32 -> PC, a cada 2 s:   L,<tds>,<turbidez>,<temperatura>     ex.: L,54.30,147.00,24.10

    PC -> STM32 (comandos):    RELE_ON            liga a bomba
                               RELE_OFF           desliga a bomba
                               LIMITE,<TDS|TURBIDEZ|TEMPERATURA>,<valor>   muda o limite de corte daquele sensor
    STM32 -> PC (confirmação): A,RELE_ON          a placa executou o comando
                               A,RELE_OFF
                               A,LIMITE,<sensor>,<valor>                   ex.: A,LIMITE,TDS,250.00
    STM32 -> PC (eventos, a placa avisa sozinha, sem o PC pedir):
                               E,LIMITE,<TDS|TURBIDEZ|TEMPERATURA>   a placa cortou a bomba: sensor acima do limite
                               E,BOTAO                               botão de emergência físico apertado: bomba cortada
                               E,BLOQUEADO                           RELE_ON recusado (acima do limite ou botão apertado)

    HANDSHAKE (antes de qualquer leitura; a placa liga calada, esperando, a 9600 bps):

    PC -> STM32:               HELLO                  "tem alguém aí?" (a placa responde em QUALQUER estado)
    STM32 -> PC:               H,READY,<fw>,<bauds>   ex.: H,READY,5.0,9600;19200;38400;115200
    PC -> STM32:               BAUD,<n>               "vamos conversar a <n> bps" (n tem de estar na lista da placa)
    STM32 -> PC:               H,BAUD,<n>             aceito (enviado ainda no baud antigo; depois os dois trocam)
                               H,ERRO,BAUD            recusado (baud fora da lista): segue tudo a 9600
    PC -> STM32:               START                  "pode mandar as leituras"
    STM32 -> PC:               H,START                a placa entra em TELEMETRIA e começa a mandar as linhas "L,"
                               H,ERRO,<motivo>        ESTADO / COMANDO

    Sem handshake a placa não manda leituras e recusa RELE_ON e LIMITE (RELE_OFF vale sempre).

Uma linha corrompida NUNCA derruba o programa: interpretar_leitura() levanta
LinhaInvalida e quem chamou decide ignorar (atividade 1 da aula de 09/11).
"""
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from models.data_simulator import calcular_iqa
from models.sensor_data import LeituraTelemetria

PREFIXO_LEITURA = "L,"
CAMPOS_LEITURA = 3  # tds, turbidez, temperatura

# DS18B20 desconectado/com defeito devolve -127 °C (valor de erro da biblioteca DallasTemperature)
TEMPERATURA_SEM_SENSOR = -127.0
TEMPERATURA_MIN_C, TEMPERATURA_MAX_C = -55.0, 125.0  # faixa de medição do DS18B20

# ---------- comandos (PC -> STM32) ----------
CMD_RELE_ON = "RELE_ON"
CMD_RELE_OFF = "RELE_OFF"
COMANDOS_VALIDOS = (CMD_RELE_ON, CMD_RELE_OFF)

# ---------- confirmações e eventos (STM32 -> PC) ----------
PREFIXO_CONFIRMACAO = "A,"
PREFIXO_EVENTO = "E,"
SENSORES_DO_LIMITE = ("TDS", "TURBIDEZ", "TEMPERATURA")

# ---------- limites de corte (PC -> STM32) ----------
CMD_LIMITE = "LIMITE"
LIMITE_MAXIMO_ACEITO = 10000.0  # igual ao LIMITE_MAXIMO_ACEITO do firmware: acima disso a placa ignora


# ---------- handshake e negociação de baud ----------
PREFIXO_HANDSHAKE = "H,"
CMD_HELLO = "HELLO"
CMD_BAUD = "BAUD"
CMD_START = "START"
BAUD_INICIAL = 9600                          # toda conversa começa aqui (a placa liga a 9600)
BAUDS_SUPORTADOS = (9600, 19200, 38400, 115200)  # o que o SCADA sabe falar; a placa avisa os dela no H,READY
SEPARADOR_BAUDS = ";"                        # a vírgula já separa os campos da linha


class TipoHandshake(str, Enum):
    READY = "READY"  # resposta ao HELLO: firmware e bauds aceitos
    BAUD = "BAUD"    # a placa aceitou o baud pedido
    START = "START"  # a placa começou a mandar leituras
    ERRO = "ERRO"    # a placa recusou (motivo no campo 'motivo')


@dataclass(frozen=True)
class MensagemHandshake:
    tipo: TipoHandshake
    firmware: str | None = None
    bauds: tuple[int, ...] = ()
    baud: int | None = None
    motivo: str | None = None


class TipoEventoPlaca(str, Enum):
    LIMITE = "LIMITE"        # a placa cortou a bomba porque um sensor passou do limite
    BOTAO = "BOTAO"          # botão de emergência físico apertado: a placa cortou a bomba
    BLOQUEADO = "BLOQUEADO"  # a placa recusou o RELE_ON


@dataclass(frozen=True)
class EventoPlaca:
    tipo: TipoEventoPlaca
    sensor: str | None = None  # só no LIMITE: "TDS", "TURBIDEZ" ou "TEMPERATURA"


@dataclass(frozen=True)
class ConfirmacaoLimite:
    """A placa aplicou um novo limite de corte (linha 'A,LIMITE,<sensor>,<valor>')."""
    sensor: str
    valor: float


class LinhaInvalida(ValueError):
    """Linha com prefixo conhecido ('L,', 'A,' ou 'E,') mas corrompida (campos faltando, texto no lugar de número...)."""


class SensorSemLeitura(ValueError):
    """Linha bem formada, mas o DS18B20 devolveu -127 (sensor desconectado)."""


def eh_linha_de_leitura(texto: str) -> bool:
    return texto.startswith(PREFIXO_LEITURA)


def eh_linha_de_confirmacao(texto: str) -> bool:
    return texto.startswith(PREFIXO_CONFIRMACAO)


def eh_linha_de_evento(texto: str) -> bool:
    return texto.startswith(PREFIXO_EVENTO)


def interpretar_leitura(texto: str, timestamp=None) -> LeituraTelemetria:
    """Converte 'L,54.30,147.00,24.10' em LeituraTelemetria. Levanta LinhaInvalida / SensorSemLeitura."""
    texto = texto.strip()
    if not eh_linha_de_leitura(texto):
        raise LinhaInvalida(f"não é uma linha de leitura: {texto!r}")

    campos = texto[len(PREFIXO_LEITURA):].split(",")
    if len(campos) != CAMPOS_LEITURA:
        raise LinhaInvalida(f"esperava {CAMPOS_LEITURA} campos (tds, turbidez, temperatura): {texto!r}")

    try:
        tds, turbidez, temperatura = (float(campo) for campo in campos)
    except ValueError:
        raise LinhaInvalida(f"campo não numérico: {texto!r}") from None

    if not all(math.isfinite(valor) for valor in (tds, turbidez, temperatura)):
        raise LinhaInvalida(f"valor nan/inf: {texto!r}")
    if temperatura == TEMPERATURA_SEM_SENSOR:
        raise SensorSemLeitura("DS18B20 devolveu -127 °C")
    if not TEMPERATURA_MIN_C <= temperatura <= TEMPERATURA_MAX_C:
        raise LinhaInvalida(f"temperatura fora da faixa do DS18B20: {texto!r}")
    if tds < 0 or turbidez < 0:
        raise LinhaInvalida(f"valor negativo: {texto!r}")

    return LeituraTelemetria(
        timestamp=timestamp or datetime.now(),
        temperatura_c=round(temperatura, 1),
        tds_ppm=round(tds, 1),
        # por enquanto é o índice bruto do sensor (ainda sem calibração em NTU)
        turbidez_ntu=round(turbidez, 2),
        iqa=calcular_iqa(tds, turbidez),
    )


def interpretar_confirmacao(texto: str) -> str:
    """Converte 'A,RELE_OFF' no nome do comando confirmado ('RELE_OFF'). Levanta LinhaInvalida."""
    texto = texto.strip()
    if not eh_linha_de_confirmacao(texto):
        raise LinhaInvalida(f"não é uma linha de confirmação: {texto!r}")
    comando = texto[len(PREFIXO_CONFIRMACAO):]
    if comando not in COMANDOS_VALIDOS:
        raise LinhaInvalida(f"comando desconhecido na confirmação: {texto!r}")
    return comando


def interpretar_evento(texto: str) -> EventoPlaca:
    """Converte 'E,LIMITE,TDS', 'E,BOTAO' ou 'E,BLOQUEADO' em EventoPlaca. Levanta LinhaInvalida."""
    texto = texto.strip()
    if not eh_linha_de_evento(texto):
        raise LinhaInvalida(f"não é uma linha de evento: {texto!r}")

    campos = texto[len(PREFIXO_EVENTO):].split(",")
    try:
        tipo = TipoEventoPlaca(campos[0])
    except ValueError:
        raise LinhaInvalida(f"evento desconhecido: {texto!r}") from None

    if tipo is TipoEventoPlaca.LIMITE:
        if len(campos) != 2 or campos[1] not in SENSORES_DO_LIMITE:
            raise LinhaInvalida(f"E,LIMITE precisa de um sensor ({', '.join(SENSORES_DO_LIMITE)}): {texto!r}")
        return EventoPlaca(tipo, campos[1])

    if len(campos) != 1:
        raise LinhaInvalida(f"o evento {tipo.value} não tem campos extras: {texto!r}")
    return EventoPlaca(tipo)


def montar_comando_limite(sensor: str, valor: float) -> str:
    """Monta 'LIMITE,TDS,250.00' (sem a quebra de linha). Levanta ValueError se a placa recusaria."""
    if sensor not in SENSORES_DO_LIMITE:
        raise ValueError(f"sensor desconhecido: {sensor!r}")
    if not math.isfinite(valor) or valor <= 0 or valor > LIMITE_MAXIMO_ACEITO:
        raise ValueError(f"limite fora da faixa aceita pela placa (0 a {LIMITE_MAXIMO_ACEITO:.0f}): {valor!r}")
    return f"{CMD_LIMITE},{sensor},{valor:.2f}"


def interpretar_confirmacao_limite(texto: str) -> ConfirmacaoLimite:
    """Converte 'A,LIMITE,TDS,250.00' em ConfirmacaoLimite. Levanta LinhaInvalida."""
    texto = texto.strip()
    if not eh_linha_de_confirmacao(texto):
        raise LinhaInvalida(f"não é uma linha de confirmação: {texto!r}")

    campos = texto[len(PREFIXO_CONFIRMACAO):].split(",")
    if len(campos) != 3 or campos[0] != CMD_LIMITE or campos[1] not in SENSORES_DO_LIMITE:
        raise LinhaInvalida(f"esperava A,LIMITE,<sensor>,<valor>: {texto!r}")
    try:
        valor = float(campos[2])
    except ValueError:
        raise LinhaInvalida(f"valor não numérico na confirmação do limite: {texto!r}") from None
    if not math.isfinite(valor) or valor <= 0:
        raise LinhaInvalida(f"valor inválido na confirmação do limite: {texto!r}")
    return ConfirmacaoLimite(campos[1], valor)


# ----------------------------------------------------------------------
# Handshake e negociação de baud
# ----------------------------------------------------------------------
def eh_linha_de_handshake(texto: str) -> bool:
    return texto.startswith(PREFIXO_HANDSHAKE)


def _inteiro_positivo(texto: str) -> int:
    """Só dígitos e maior que zero ('9600'): recusa '', '0', '-1', '96.0', '9 600', '+5', '1e3'. Levanta ValueError."""
    if not texto.isascii() or not texto.isdigit() or int(texto) <= 0:
        raise ValueError(texto)
    return int(texto)


def interpretar_handshake(texto: str) -> MensagemHandshake:
    """Converte 'H,READY,5.0,9600;115200', 'H,BAUD,115200', 'H,START' ou 'H,ERRO,BAUD'. Levanta LinhaInvalida."""
    texto = texto.strip()
    if not eh_linha_de_handshake(texto):
        raise LinhaInvalida(f"não é uma linha de handshake: {texto!r}")

    campos = texto[len(PREFIXO_HANDSHAKE):].split(",")
    try:
        tipo = TipoHandshake(campos[0])
    except ValueError:
        raise LinhaInvalida(f"mensagem de handshake desconhecida: {texto!r}") from None

    if tipo is TipoHandshake.READY:
        if len(campos) != 3 or not campos[1]:
            raise LinhaInvalida(f"esperava H,READY,<firmware>,<bauds>: {texto!r}")
        try:
            bauds = tuple(_inteiro_positivo(b) for b in campos[2].split(SEPARADOR_BAUDS))
        except ValueError:
            raise LinhaInvalida(f"lista de bauds inválida no H,READY: {texto!r}") from None
        return MensagemHandshake(tipo, firmware=campos[1], bauds=bauds)

    if tipo is TipoHandshake.BAUD:
        if len(campos) != 2:
            raise LinhaInvalida(f"esperava H,BAUD,<n>: {texto!r}")
        try:
            baud = _inteiro_positivo(campos[1])
        except ValueError:
            raise LinhaInvalida(f"baud inválido no H,BAUD: {texto!r}") from None
        return MensagemHandshake(tipo, baud=baud)

    if tipo is TipoHandshake.ERRO:
        if len(campos) != 2 or not campos[1]:
            raise LinhaInvalida(f"esperava H,ERRO,<motivo>: {texto!r}")
        return MensagemHandshake(tipo, motivo=campos[1])

    if len(campos) != 1:  # H,START não tem campos extras
        raise LinhaInvalida(f"o H,START não tem campos extras: {texto!r}")
    return MensagemHandshake(tipo)


def montar_comando_baud(baud: int) -> str:
    """Monta 'BAUD,115200' (sem a quebra de linha). Levanta ValueError se o SCADA não fala esse baud."""
    if type(baud) is not int or baud not in BAUDS_SUPORTADOS:  # int de verdade: nada de 9600.0 nem True
        raise ValueError(f"baud não suportado: {baud!r} (use um de {', '.join(map(str, BAUDS_SUPORTADOS))})")
    return f"{CMD_BAUD},{baud}"


def escolher_baud(desejado: int, bauds_da_placa, bauds_do_pc=BAUDS_SUPORTADOS) -> int:
    """
    Negociação: o baud que os DOIS lados sabem falar e que mais se aproxima do desejado (sem passar dele).

      - desejado está na lista dos dois        -> usa o desejado
      - não está                               -> o maior comum que não passa do desejado
      - nada comum abaixo do desejado          -> o menor comum
      - nenhum em comum                        -> BAUD_INICIAL (a conversa já está acontecendo a 9600)
    """
    comuns = (set(bauds_da_placa) & set(bauds_do_pc)) | {BAUD_INICIAL}
    if desejado in comuns:
        return desejado
    abaixo = [b for b in comuns if b <= desejado]
    return max(abaixo) if abaixo else min(comuns)
