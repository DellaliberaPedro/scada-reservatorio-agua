"""
Controller da tela (QDialog) de Histórico de Eventos.

Os dados vêm do SQLite (tabela eventos), mas esta janela NÃO abre o banco: ela pede a
consulta à thread do banco (models/database_worker.py) e recebe a lista por callback:
  - no showEvent (toda vez que a janela é exibida);
  - a cada poucos segundos enquanto ela estiver aberta, para mostrar em
    tempo real os eventos que o MainController acabou de gravar.
Enquanto a consulta roda, a janela continua respondendo (nada de E/S na GUI thread).
"""
import logging

from PySide6.QtCore import QDate, QTimer
from PySide6.QtWidgets import QDialog, QHeaderView, QMessageBox, QTableWidgetItem

from ui.Ui_tela_historico import Ui_HistoricoDialog

logger = logging.getLogger(__name__)

INTERVALO_ATUALIZACAO_MS = 2000


class HistoricoController(QDialog):
    """
    Janela modal que exibe o histórico de eventos do sistema
    (cortes de emergência, ultrapassagem de limites, trocas de estado
    da válvula) em uma tabela filtrável por data.
    """

    def __init__(self, banco, parent=None):
        super().__init__(parent)
        self.ui = Ui_HistoricoDialog()
        self.ui.setupUi(self)

        self._banco = banco          # BancoDeDados: fachada da thread do banco
        self._consultando = False    # True enquanto uma consulta espera a resposta (não empilha pedidos)
        self._ativa = False          # True só com a janela exibida (resposta tardia é descartada)

        # largura pelo conteúdo, exceto a coluna de descrição (índice 2), que estica
        header = self.ui.table_historico.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)

        self.ui.date_filtro_historico.setDate(QDate.currentDate())
        self.ui.date_filtro_historico.dateChanged.connect(self._atualizar_tabela)
        self.ui.buttonBox.rejected.connect(self.reject)
        self.ui.buttonBox.accepted.connect(self.accept)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._atualizar_tabela)

    def showEvent(self, event):
        """Carrega os eventos do banco sempre que a janela aparece e liga a atualização automática."""
        super().showEvent(event)
        self._ativa = True
        self._atualizar_tabela()
        self.timer.start(INTERVALO_ATUALIZACAO_MS)

    def hideEvent(self, event):
        self._ativa = False
        self.timer.stop()
        super().hideEvent(event)

    def _atualizar_tabela(self):
        """Pede os eventos à thread do banco. A tabela é redesenhada em _ao_eventos_listados()."""
        if self._consultando:
            return  # a consulta anterior ainda não voltou: não empilha pedidos
        self._consultando = True
        self._banco.executar("listar_eventos", ao_terminar=self._ao_eventos_listados)

    def _ao_eventos_listados(self, resultado):
        """
        GUI thread, quando a thread do banco terminar. Repopula a tabela com os eventos cuja data seja
        igual ou posterior à data do filtro. Mantém a rolagem se houver linha selecionada; senão, rola
        até o evento mais recente.
        """
        self._consultando = False
        if not self._ativa:
            return  # a janela fechou enquanto a consulta rodava
        if not resultado.ok:
            self.timer.stop()  # evita uma caixa de erro a cada 2 s
            logger.error("Falha ao carregar o histórico do SQLite: %s", resultado.erro)
            QMessageBox.critical(self, "Banco de dados", f"Não foi possível carregar o histórico.\n{resultado.erro}")
            return
        eventos = resultado.valor

        data_filtro = self.ui.date_filtro_historico.date().toPython()
        linha_selecionada = self.ui.table_historico.currentRow()

        self.ui.table_historico.setRowCount(0)
        for registro in eventos:
            if registro.timestamp.date() >= data_filtro:
                linha = self.ui.table_historico.rowCount()
                self.ui.table_historico.insertRow(linha)
                for coluna, texto in enumerate(registro.como_linha()):
                    self.ui.table_historico.setItem(linha, coluna, QTableWidgetItem(texto))

        if linha_selecionada < 0:
            self.ui.table_historico.scrollToBottom()
