"""
Handshake e negociação de baud (parte pura: sem PySide6, sem pyserial e sem relógio).

O SerialWorker só faz a E/S (abrir a porta, escrever, ler, esperar). Quem decide O QUE mandar e o que
cada resposta significa é esta máquina de estados, por isso dá para testá-la linha por linha.

    INICIAL ──iniciar()──> HELLO ──H,READY──> BAUD ──H,BAUD,<n>──> START ──H,START──> CONCLUIDO
                             │                  │                    │
                             │                  └─H,ERRO,BAUD──> START (segue a 9600: fallback)
                             └─ (se o baud escolhido já é 9600, pula direto para START)

    Qualquer estado de espera que estoura o tempo reenvia o último comando; esgotadas as tentativas -> FALHOU.

Cada chamada devolve um Passo: o que o worker deve fazer agora (trocar o baud da porta, enviar uma linha,
avisar o operador, concluir ou desistir). Linhas que não fazem sentido naquele estado (L, de uma placa
que já estava transmitindo, H,READY repetido por causa de um HELLO reenviado...) devolvem None: são ignoradas.
"""
from dataclasses import dataclass
from enum import Enum

from models.serial_protocol import (
    BAUD_INICIAL,
    BAUDS_SUPORTADOS,
    CMD_HELLO,
    CMD_START,
    LinhaInvalida,
    MensagemHandshake,
    TipoHandshake,
    eh_linha_de_handshake,
    escolher_baud,
    interpretar_handshake,
    montar_comando_baud,
)

TENTATIVAS_HELLO = 4   # a placa pode estar no meio de uma leitura (~1,3 s) e demorar a ouvir
TENTATIVAS_PASSO = 3   # BAUD e START


class EstadoHandshake(str, Enum):
    INICIAL = "INICIAL"
    HELLO = "HELLO"          # mandou HELLO, espera o H,READY
    BAUD = "BAUD"            # mandou BAUD,<n>, espera o H,BAUD,<n>
    START = "START"          # mandou START, espera o H,START
    CONCLUIDO = "CONCLUIDO"
    FALHOU = "FALHOU"


@dataclass(frozen=True)
class Passo:
    """O que o worker deve fazer agora. Os campos são aplicados NESTA ordem: trocar_baud, enviar, depois o resto."""
    enviar: str | None = None       # linha a mandar (sem a quebra de linha)
    trocar_baud: int | None = None  # reconfigurar a porta do PC para este baud ANTES de enviar
    aviso: str | None = None        # texto para o operador/log (informação, não é erro)
    concluido: bool = False         # handshake terminou: a placa vai começar a mandar leituras
    falha: str | None = None        # motivo da desistência


class Handshake:
    def __init__(self, baud_desejado, bauds_do_pc=BAUDS_SUPORTADOS):
        self.baud_desejado = int(baud_desejado)
        self.bauds_do_pc = tuple(bauds_do_pc)
        self.estado = EstadoHandshake.INICIAL
        self.baud_atual = BAUD_INICIAL    # o baud em que a porta está agora
        self.baud_proposto = None         # o que foi pedido no BAUD,<n> (enquanto espera a resposta)
        self.firmware = None
        self.bauds_da_placa = ()
        self.fallback = False             # True = a placa recusou o baud e a conversa segue a 9600
        self._tentativas = 0
        self._ultimo_envio = None

    # ------------------------------------------------------------------
    @property
    def baud_negociado(self):
        """O baud final da conversa (vale depois de CONCLUIDO)."""
        return self.baud_atual

    @property
    def terminou(self):
        return self.estado in (EstadoHandshake.CONCLUIDO, EstadoHandshake.FALHOU)

    # ------------------------------------------------------------------
    def iniciar(self) -> Passo:
        """Primeiro passo: a porta acabou de abrir a 9600. Manda HELLO."""
        self.estado = EstadoHandshake.HELLO
        self._tentativas = 1
        return self._enviar(CMD_HELLO)

    def receber(self, texto: str) -> Passo | None:
        """Uma linha chegou da placa. Devolve o próximo passo, ou None se a linha não importa agora."""
        if self.terminou or self.estado is EstadoHandshake.INICIAL:
            return None
        texto = texto.strip()
        if not eh_linha_de_handshake(texto):
            return None  # "L," de uma placa que já transmitia, "A,", "E,", lixo...
        try:
            msg = interpretar_handshake(texto)
        except LinhaInvalida:
            return None

        if self.estado is EstadoHandshake.HELLO:
            return self._no_hello(msg)
        if self.estado is EstadoHandshake.BAUD:
            return self._no_baud(msg)
        return self._no_start(msg)

    def estourou_tempo(self) -> Passo:
        """Ninguém respondeu a tempo: reenvia o último comando ou desiste."""
        if self.terminou or self.estado is EstadoHandshake.INICIAL:
            return Passo()
        limite = TENTATIVAS_HELLO if self.estado is EstadoHandshake.HELLO else TENTATIVAS_PASSO
        if self._tentativas >= limite:
            return self._falhar(self._motivo_do_tempo())
        self._tentativas += 1
        return Passo(enviar=self._ultimo_envio)

    # ------------------------------------------------------------------
    def _no_hello(self, msg: MensagemHandshake) -> Passo | None:
        if msg.tipo is not TipoHandshake.READY:
            return None
        self.firmware = msg.firmware
        self.bauds_da_placa = msg.bauds
        alvo = escolher_baud(self.baud_desejado, msg.bauds, self.bauds_do_pc)
        aviso = None
        if alvo != self.baud_desejado:
            aviso = (f"A placa (firmware {msg.firmware}) não fala {self.baud_desejado} bps: "
                     f"negociado {alvo} bps no lugar.")
        if alvo == self.baud_atual:  # já está no baud certo (9600): não precisa trocar
            self.estado = EstadoHandshake.START
            self._tentativas = 1
            passo = self._enviar(CMD_START)
            return Passo(enviar=passo.enviar, aviso=aviso)
        self.estado = EstadoHandshake.BAUD
        self._tentativas = 1
        self.baud_proposto = alvo
        passo = self._enviar(montar_comando_baud(alvo))
        return Passo(enviar=passo.enviar, aviso=aviso)

    def _no_baud(self, msg: MensagemHandshake) -> Passo | None:
        if msg.tipo is TipoHandshake.BAUD and msg.baud == self.baud_proposto:
            self.baud_atual = msg.baud  # a placa aceitou: os dois lados trocam agora
            self.estado = EstadoHandshake.START
            self._tentativas = 1
            passo = self._enviar(CMD_START)
            return Passo(trocar_baud=self.baud_atual, enviar=passo.enviar)
        if msg.tipo is TipoHandshake.ERRO:
            # a placa recusou o baud: segue a 9600, que já funciona (fallback)
            self.fallback = True
            self.baud_proposto = None
            self.estado = EstadoHandshake.START
            self._tentativas = 1
            passo = self._enviar(CMD_START)
            return Passo(
                enviar=passo.enviar,
                aviso=f"A placa recusou a troca de baud ({msg.motivo}): a comunicação continua a {self.baud_atual} bps.",
            )
        return None  # H,READY repetido (HELLO reenviado) ou H,BAUD de outro valor: ignora

    def _no_start(self, msg: MensagemHandshake) -> Passo | None:
        if msg.tipo is TipoHandshake.START:
            self.estado = EstadoHandshake.CONCLUIDO
            return Passo(concluido=True)
        if msg.tipo is TipoHandshake.ERRO:
            return self._falhar(f"a placa recusou o START ({msg.motivo})")
        return None

    # ------------------------------------------------------------------
    def _enviar(self, linha: str) -> Passo:
        self._ultimo_envio = linha
        return Passo(enviar=linha)

    def _falhar(self, motivo: str) -> Passo:
        self.estado = EstadoHandshake.FALHOU
        return Passo(falha=motivo)

    def _motivo_do_tempo(self) -> str:
        if self.estado is EstadoHandshake.HELLO:
            return f"a placa não respondeu ao HELLO a {self.baud_atual} bps"
        if self.estado is EstadoHandshake.BAUD:
            return f"a placa não confirmou a troca para {self.baud_proposto} bps"
        return "a placa não confirmou o START"
