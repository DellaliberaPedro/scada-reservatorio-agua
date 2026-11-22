"""
Thread de banco de dados (Database Thread): toda E/S de SQLite do programa EM EXECUÇÃO passa por aqui.

Três papéis, três threads:

    GUI thread     desenha a tela e trata cliques. Nunca abre o SQLite depois que a janela está no ar.
    Serial thread  SerialWorker (models/serial_worker.py): lê o STM32 e escreve os comandos.
    Banco thread   DatabaseWorker (este arquivo): grava e consulta o SQLite.

Como as threads conversam (nenhuma toca no objeto da outra):

    GUI ---- fila queue.Queue (thread-safe, FIFO) ----> DatabaseWorker.executar()   [thread do banco]
    GUI <--- sinal concluido(token, resultado) -------- DatabaseWorker              [fila de eventos do Qt]

  - A GUI chama BancoDeDados.executar("nome_da_operacao", args..., ao_terminar=funcao) e a chamada
    volta NA HORA: só coloca um pedido na fila. A tela nunca espera o disco.
  - A thread do banco tira os pedidos da fila na ordem em que chegaram (FIFO), executa cada um num
    repositório (SQL com '?', uma transação por operação) e emite concluido(token, resultado).
  - O sinal chega na GUI thread pela fila de eventos do Qt; a fachada procura a função que o pedido
    deixou (ao_terminar) e a chama já na GUI thread, onde é seguro mexer em widget.
  - Só operações da lista OPERACOES são aceitas (nada de SQL vindo de fora).
  - Os argumentos são COPIADOS antes de entrar na fila: a GUI e a thread do banco nunca dividem o
    mesmo objeto mutável (o repositório preenche evento.id; a GUI continua usando o dela).
  - Desligar: BancoDeDados.encerrar() põe um marcador (_FIM) no fim da fila. Como a fila é FIFO, tudo
    que já foi pedido é gravado antes de a thread terminar (nenhum evento se perde ao fechar a janela).

Não importa se uma operação falha: o erro vira um ResultadoBanco(ok=False) e a thread continua viva.
"""
import copy
import logging
import queue
import sqlite3
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, Signal

from models.dispositivo_repository import DispositivoRepository
from models.evento_repository import EventoRepository
from models.leitura_repository import LeituraRepository
from models.regra_repository import RegraRepository

logger = logging.getLogger(__name__)

ESPERA_ENCERRAR_MS = 5000  # quanto tempo o fechamento da janela espera a fila esvaziar

# marcador de fim de fila (identidade, nunca confundido com um pedido)
_FIM = object()


@dataclass(frozen=True)
class ResultadoBanco:
    """O que a thread do banco devolve para a GUI."""
    ok: bool
    valor: object = None   # retorno da operação (ex.: lista de eventos)
    erro: str = ""         # texto do erro quando ok=False
    detalhe: str = ""      # informação extra de diagnóstico (ex.: estado gravado depois de um rollback)


class FalhaNoBanco(Exception):
    """Falha de uma operação, com um detalhe de diagnóstico para o log."""

    def __init__(self, mensagem, detalhe=""):
        super().__init__(mensagem)
        self.detalhe = detalhe


# ----------------------------------------------------------------------
# Operações permitidas (rodam SEMPRE na thread do banco)
# ----------------------------------------------------------------------
def _estados_gravados():
    """Lê o que ficou gravado em dispositivos (prova do rollback, vai para o log)."""
    try:
        return ", ".join(f"{d.tag}={d.status.value}" for d in DispositivoRepository.listar_todos())
    except sqlite3.Error as erro:
        return f"indisponível ({erro})"


def _inserir_evento(evento):
    return EventoRepository.inserir(evento)


def _alterar_estados(novos_status, evento, simular_falha=False):
    """Válvula + relé + evento numa ÚNICA transação (tudo ou nada). Falhou? Rollback e o estado que ficou."""
    try:
        DispositivoRepository.alterar_estados_com_evento(novos_status, evento, simular_falha=simular_falha)
    except (sqlite3.Error, RuntimeError, LookupError) as erro:
        raise FalhaNoBanco(str(erro), detalhe=_estados_gravados()) from erro


def _atualizar_regra(regra):
    if regra.id is None or not RegraRepository.atualizar(regra):
        raise LookupError("regra não encontrada no banco")


def _substituir_regras(regras):
    """Troca o conjunto de regras numa transação e devolve o que ficou gravado."""
    RegraRepository.substituir_todas(regras)
    return RegraRepository.listar_todos()


def _listar_eventos():
    return EventoRepository.listar_todos()


def _inserir_leitura(leitura):
    return LeituraRepository.inserir(leitura)


def _consultar_leituras(inicio, fim, max_pontos):
    """Histórico de leituras de um período, já agrupado em no máximo max_pontos pontos (ver LeituraRepository)."""
    return LeituraRepository.consultar_periodo(inicio, fim, max_pontos)


OPERACOES = {
    "inserir_evento": _inserir_evento,
    "alterar_estados": _alterar_estados,
    "atualizar_regra": _atualizar_regra,
    "substituir_regras": _substituir_regras,
    "listar_eventos": _listar_eventos,
    "inserir_leitura": _inserir_leitura,
    "consultar_leituras": _consultar_leituras,
}


def executar_operacao(nome, args):
    """
    Roda UMA operação e devolve um ResultadoBanco. Nunca levanta: um erro de banco (ou um bug) vira
    ok=False, para a thread do banco continuar atendendo os próximos pedidos.
    """
    funcao = OPERACOES.get(nome)
    if funcao is None:
        logger.error("Operação de banco desconhecida: %r", nome)
        return ResultadoBanco(ok=False, erro=f"operação de banco desconhecida: {nome}")
    try:
        return ResultadoBanco(ok=True, valor=funcao(*args))
    except FalhaNoBanco as erro:
        return ResultadoBanco(ok=False, erro=str(erro), detalhe=erro.detalhe)
    except (sqlite3.Error, OSError, RuntimeError, LookupError, ValueError) as erro:
        return ResultadoBanco(ok=False, erro=str(erro))
    except Exception as erro:  # um bug numa operação não pode derrubar a thread do banco
        logger.exception("Erro inesperado na operação de banco %s: %s", nome, erro)
        return ResultadoBanco(ok=False, erro=f"erro inesperado: {erro}")


# ----------------------------------------------------------------------
# Worker (vive na QThread do banco)
# ----------------------------------------------------------------------
class DatabaseWorker(QObject):
    concluido = Signal(int, object)  # token do pedido, ResultadoBanco (chega na GUI thread)
    finalizado = Signal()

    def __init__(self):
        super().__init__()
        self._fila = queue.Queue()

    def enfileirar(self, token, operacao, args):
        """Chamado pela GUI thread. Só coloca o pedido na fila: nunca espera o banco."""
        self._fila.put((token, operacao, args))

    def parar(self):
        """Marcador de fim: tudo que já está na fila ainda é gravado antes de a thread terminar."""
        self._fila.put(_FIM)

    def executar(self):
        """thread.started -> executar. Atende a fila até chegar o marcador de fim."""
        logger.info("Thread de banco iniciada (thread id %s).", threading.get_ident())
        try:
            while True:
                pedido = self._fila.get()
                if pedido is _FIM:
                    break
                token, operacao, args = pedido
                self.concluido.emit(token, executar_operacao(operacao, args))
        finally:
            logger.info("Thread de banco encerrada.")
            self.finalizado.emit()


# ----------------------------------------------------------------------
# Fachada usada pela GUI
# ----------------------------------------------------------------------
class BancoDeDados(QObject):
    """
    Lado da GUI: cria a QThread + DatabaseWorker (padrão da aula de 26/10) e entrega os resultados.
    Todos os métodos daqui rodam na GUI thread.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None
        self._callbacks = {}    # token -> função a chamar quando o resultado chegar
        self._proximo_token = 0

    def iniciar(self):
        if self._thread is not None:
            return
        self._thread = QThread()
        self._thread.setObjectName("thread-banco")
        self._worker = DatabaseWorker()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.executar)
        self._worker.concluido.connect(self._ao_concluir)  # slot na GUI thread
        self._worker.finalizado.connect(self._thread.quit)
        self._thread.start()

    def executar(self, operacao, *args, ao_terminar=None):
        """
        Pede uma operação à thread do banco e VOLTA NA HORA (devolve o token do pedido).
        Se ao_terminar for dado, será chamada como ao_terminar(ResultadoBanco) na GUI thread.
        """
        if operacao not in OPERACOES:
            raise ValueError(f"operação de banco desconhecida: {operacao!r}")
        if self._worker is None:
            raise RuntimeError("a thread do banco não foi iniciada (chame iniciar() antes)")

        self._proximo_token += 1
        token = self._proximo_token
        if ao_terminar is not None:
            self._callbacks[token] = ao_terminar
        # cópia: a GUI e a thread do banco não dividem objetos mutáveis
        self._worker.enfileirar(token, operacao, copy.deepcopy(args))
        return token

    def _ao_concluir(self, token, resultado):
        """Slot (GUI thread): devolve o resultado a quem pediu."""
        callback = self._callbacks.pop(token, None)
        if callback is not None:
            callback(resultado)

    def encerrar(self, espera_ms=ESPERA_ENCERRAR_MS):
        """Fechar a janela: grava o que ainda está na fila e espera a thread terminar."""
        if self._worker is None:
            return
        self._worker.parar()
        if self._thread is not None:
            # quit() direto (como o closeEvent da serial): a GUI vai ficar presa no wait() e não
            # processaria o sinal finalizado -> quit. A thread só sai depois de esvaziar a fila.
            self._thread.quit()
            self._thread.wait(espera_ms)
