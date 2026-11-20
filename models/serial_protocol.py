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
