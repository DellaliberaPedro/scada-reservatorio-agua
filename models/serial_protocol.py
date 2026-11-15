"""
Protocolo serial entre o STM32 e o supervisório (parte pura: sem PySide6 e sem pyserial).

Tudo é texto ASCII terminado em quebra de linha, como no protocolo usado nas aulas de serial:

    STM32 -> PC, a cada 2 s:   L,<tds>,<turbidez>,<temperatura>     ex.: L,54.30,147.00,24.10

Uma linha corrompida NUNCA derruba o programa: interpretar_leitura() levanta
LinhaInvalida e quem chamou decide ignorar (atividade 1 da aula de 09/11).
"""
import math
from datetime import datetime

from models.data_simulator import calcular_iqa
from models.sensor_data import LeituraTelemetria

PREFIXO_LEITURA = "L,"
CAMPOS_LEITURA = 3  # tds, turbidez, temperatura

# DS18B20 desconectado/com defeito devolve -127 °C (valor de erro da biblioteca DallasTemperature)
TEMPERATURA_SEM_SENSOR = -127.0
TEMPERATURA_MIN_C, TEMPERATURA_MAX_C = -55.0, 125.0  # faixa de medição do DS18B20


class LinhaInvalida(ValueError):
    """Linha com prefixo 'L,' mas corrompida (campos faltando, texto no lugar de número...)."""


class SensorSemLeitura(ValueError):
    """Linha bem formada, mas o DS18B20 devolveu -127 (sensor desconectado)."""


def eh_linha_de_leitura(texto: str) -> bool:
    return texto.startswith(PREFIXO_LEITURA)


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
