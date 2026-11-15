"""
Worker da porta serial: lê o STM32 numa QThread separada.

Segue o padrão das aulas:
  - 26/10: Worker (QObject) + QThread + moveToThread. O Worker NUNCA encosta em widget:
           só emite sinais, e a janela (GUI thread) atualiza a tela.
  - 09/11: pyserial. list_ports.comports(), serial.Serial(timeout=1.0), espera de 2 s
           depois de abrir, reset_input_buffer(), readline(), SerialException.

Ciclo de vida do executar():

    varre as portas -> abre -> espera estabilizar (2 s) -> limpa o buffer
        -> confirma que é o STM32 (precisa chegar uma linha "L,...")
        -> lê em laço
        -> cabo saiu (SerialException) ou parou de chegar dado? fecha, avisa e volta a varrer
"""
import logging
import time

import serial
import serial.tools.list_ports
from PySide6.QtCore import QObject, Signal
from serial import SerialException

from models.serial_protocol import (
    LinhaInvalida,
    SensorSemLeitura,
    eh_linha_de_leitura,
    interpretar_leitura,
)

logger = logging.getLogger(__name__)

BAUD_PADRAO = 115200
VID_STM32 = 0x0483  # fabricante (STMicroelectronics) do "Virtual COM Port" USB da Blue Pill

TIMEOUT_LEITURA_S = 1.0        # readline() espera no máximo 1 s (igual aos exemplos da aula)
ESTABILIZACAO_S = 2.0          # pausa depois de abrir a porta (aula: DTR/bootloader; no STM32: o USB reenumera)
CONFIRMACAO_S = 6.0            # tempo para chegar uma linha "L," e provar que a porta é a do STM32
INTERVALO_VARREDURA_S = 2.0    # pausa entre varreduras enquanto não há dispositivo
PASSO_ESPERA_S = 0.1           # as esperas são fatiadas: o botão Desconectar responde rápido
TIMEOUT_SEM_DADOS_MIN_S = 5.0  # o STM32 manda 1 linha a cada 2 s; menos que isso daria falso alarme

MOTIVO_OPERADOR = "desconectado pelo operador"
MOTIVO_CABO = "dispositivo desconectado fisicamente"


class SerialWorker(QObject):
    # Worker -> interface (chegam na GUI thread pela fila de eventos do Qt)
    status_msg = Signal(str)           # andamento da busca: "Testando COM5..."
    portas_atualizadas = Signal(list)  # portas vistas na varredura (alimenta o combo)
    conectado = Signal(str, int)       # porta, baud
    desconectado = Signal(str)         # motivo
    leitura_recebida = Signal(object)  # LeituraTelemetria
    aviso = Signal(str, bool)          # texto, é_problema (True = alerta, False = informação)
    finalizado = Signal()

    def __init__(self, baud_rate=BAUD_PADRAO, porta_fixa=None, timeout_sem_dados_s=TIMEOUT_SEM_DADOS_MIN_S):
        super().__init__()
        self._baud = int(baud_rate)
        self._porta_fixa = porta_fixa  # None = automático (varre as portas e acha o STM32)
        self._timeout_sem_dados_s = max(float(timeout_sem_dados_s), TIMEOUT_SEM_DADOS_MIN_S)
        self._parar = False
        self._descartadas = set()  # portas que abriram mas NÃO são o STM32 (não reabrir outros aparelhos a cada varredura)
        self._ultimas_portas = None
        self._sem_sensor_temp = False

    # ------------------------------------------------------------------
    # Controle (chamado pela janela)
    # ------------------------------------------------------------------
    def parar(self):
        """Interrupção cooperativa (aula 26/10): só levanta a bandeira; o laço de executar() sai sozinho."""
        self._parar = True

    # ------------------------------------------------------------------
    # Laço principal (roda na QThread)
    # ------------------------------------------------------------------
    def executar(self):
        """thread.started -> executar. Fica varrendo/lendo até pedirem para parar."""
        try:
            while not self._parar:
                conexao = None
                try:
                    conexao = self._procurar_e_abrir()
                    if conexao is None:
                        self._esperar(INTERVALO_VARREDURA_S)
                        continue

                    self.conectado.emit(conexao.port, self._baud)
                    motivo = self._ler_ate_perder(conexao)
                    self._fechar(conexao)
                    self.desconectado.emit(motivo)
                except Exception as erro:  # um bug aqui não pode matar a leitura em silêncio
                    logger.exception("Erro inesperado no worker serial: %s", erro)
                    self.aviso.emit(f"Erro inesperado na leitura serial: {erro}", True)
                    if conexao is not None:
                        self._fechar(conexao)
                    self._esperar(INTERVALO_VARREDURA_S)
        finally:
            self.finalizado.emit()

    # ------------------------------------------------------------------
    # Busca da porta
    # ------------------------------------------------------------------
    def _procurar_e_abrir(self):
        """Varre as portas e devolve a conexão já aberta e confirmada, ou None."""
        portas = serial.tools.list_ports.comports()
        dispositivos = [p.device for p in portas]
        self._descartadas &= set(dispositivos)  # porta que sumiu volta a ser candidata quando reaparecer
        if dispositivos != self._ultimas_portas:
            self._ultimas_portas = dispositivos
            self.portas_atualizadas.emit(dispositivos)

        if not portas:
            self.status_msg.emit("Nenhuma porta serial encontrada. O cabo está plugado?")
            return None

        candidatas = self._candidatas(portas)
        if not candidatas:
            if self._porta_fixa:
                self.status_msg.emit(f"Aguardando a porta {self._porta_fixa}...")
            else:
                self.status_msg.emit("Nenhum dispositivo USB serial novo. Aguardando o STM32...")
            return None

        for info in candidatas:
            if self._parar:
                return None
            conexao = self._tentar_porta(info)
            if conexao is not None:
                return conexao
        return None

    def _candidatas(self, portas):
        """Modo fixo: só a porta escolhida. Automático: portas USB serial, as da ST primeiro."""
        if self._porta_fixa:
            return [p for p in portas if p.device == self._porta_fixa]
        usb = [p for p in portas if p.vid is not None and p.device not in self._descartadas]
        return sorted(usb, key=lambda p: (p.vid != VID_STM32, p.device))

    def _tentar_porta(self, info):
        """Abre a porta e confirma que é o STM32. Devolve a conexão aberta ou None."""
        dispositivo = info.device
        self.status_msg.emit(f"Testando {dispositivo} ({info.description})...")
        try:
            conexao = serial.Serial(port=dispositivo, baudrate=self._baud, timeout=TIMEOUT_LEITURA_S)
        except SerialException as erro:
            logger.warning("Não foi possível abrir %s: %s", dispositivo, erro)
            self.status_msg.emit(f"{dispositivo} ocupada ou sem acesso. Feche o Monitor Serial/Hercules.")
            return None

        try:
            # aula: abrir a porta pode reiniciar a placa (DTR), então espera estabilizar antes de usar
            if not self._esperar(ESTABILIZACAO_S):
                self._fechar(conexao)
                return None
            conexao.reset_input_buffer()  # joga fora o lixo acumulado no boot
            conexao.reset_output_buffer()
            confirmada = self._confirmar_dispositivo(conexao)
        except (SerialException, OSError) as erro:
            logger.warning("Falha ao testar %s: %s", dispositivo, erro)
            self._fechar(conexao)
            return None

        if confirmada:
            return conexao

        self._fechar(conexao)
        if confirmada is False and not self._porta_fixa and info.vid != VID_STM32:
            logger.info("%s abriu, mas não fala o protocolo do STM32: ignorada até ser reconectada.", dispositivo)
            self._descartadas.add(dispositivo)
        return None

    def _confirmar_dispositivo(self, conexao):
        """True = é o STM32; False = ninguém falou o nosso protocolo a tempo; None = mandaram parar."""
        limite = time.monotonic() + CONFIRMACAO_S
        while time.monotonic() < limite:
            if self._parar:
                return None
            reconhecida, _ = self._interpretar(self._ler_linha(conexao))
            if reconhecida:
                return True
        return False

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------
    def _ler_ate_perder(self, conexao):
        """Laço de leitura (padrão da aula: readline + decode + strip). Devolve o motivo da saída."""
        ultimo_dado = time.monotonic()
        while not self._parar:
            try:
                texto = self._ler_linha(conexao)
            except (SerialException, OSError) as erro:
                logger.warning("Porta %s perdida: %s", conexao.port, erro)
                return MOTIVO_CABO

            if not texto:  # estourou o timeout de 1 s sem receber nada
                parado_ha = time.monotonic() - ultimo_dado
                if parado_ha >= self._timeout_sem_dados_s:
                    return f"sem dados há {parado_ha:.0f} s"
                continue

            reconhecida, leitura = self._interpretar(texto)
            if reconhecida:
                ultimo_dado = time.monotonic()
            if leitura is not None:
                self.leitura_recebida.emit(leitura)
        return MOTIVO_OPERADOR

    @staticmethod
    def _ler_linha(conexao):
        """readline() devolve b'' quando estoura o timeout; aqui vira '' (nunca levanta por causa do conteúdo)."""
        linha_bytes = conexao.readline()
        if not linha_bytes:
            return ""
        return linha_bytes.decode("utf-8", errors="replace").strip()

    def _interpretar(self, texto):
        """Devolve (reconhecida, leitura). reconhecida=True prova que o outro lado fala o nosso protocolo."""
        if not eh_linha_de_leitura(texto):
            return False, None  # vazio (timeout), mensagem de outro tipo ou lixo
        try:
            leitura = interpretar_leitura(texto)
        except SensorSemLeitura:
            if not self._sem_sensor_temp:
                self._sem_sensor_temp = True
                self.aviso.emit(
                    "Sensor de temperatura (DS18B20) sem leitura (-127): verifique a fiação e o pull-up.", True
                )
            return True, None
        except LinhaInvalida as erro:
            logger.debug("Linha serial descartada: %s", erro)
            return False, None

        if self._sem_sensor_temp:
            self._sem_sensor_temp = False
            self.aviso.emit("Sensor de temperatura voltou a responder.", False)
        return True, leitura

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------
    def _esperar(self, segundos):
        """Dorme em fatias de 0,1 s. Devolve False se pediram para parar no meio da espera."""
        fim = time.monotonic() + segundos
        while time.monotonic() < fim:
            if self._parar:
                return False
            time.sleep(PASSO_ESPERA_S)
        return not self._parar

    @staticmethod
    def _fechar(conexao):
        try:
            conexao.close()
        except (SerialException, OSError):
            pass
