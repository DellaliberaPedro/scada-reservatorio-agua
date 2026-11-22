"""
Controller da tela (QDialog) de Histórico de Leituras: gráfico e resumo de um período da série temporal.

Os dados vêm da tabela `leituras` do SQLite, mas esta janela NÃO abre o banco: ela pede a consulta à
thread do banco (models/database_worker.py, operação "consultar_leituras") e recebe o resultado por
callback na GUI thread. Enquanto a consulta roda a janela continua respondendo (nada de E/S na GUI thread).

Regras desta janela:
  - uma consulta por vez: o botão Consultar fica desabilitado até a resposta voltar;
  - período inválido (início >= fim) nunca chega ao banco;
  - se a janela fechar com a consulta no ar, a resposta tardia é descartada;
  - o banco já devolve os pontos agrupados (no máximo MAX_PONTOS_GRAFICO por curva), então 7 dias de
    leituras desenham tão rápido quanto 1 hora.
"""
import logging
from datetime import datetime, timedelta

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, QPointF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsView,
    QHeaderView,
    QMessageBox,
    QTableWidgetItem,
    QVBoxLayout,
)

from models.sensor_data import MAX_PONTOS_GRAFICO
from ui.Ui_tela_historico_leituras import Ui_HistoricoLeiturasDialog

logger = logging.getLogger(__name__)

# mesma ordem dos itens do combo_periodo; None = "Personalizado" (vale o que está nos campos De/Até)
DURACAO_POR_PERIODO = (
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
    timedelta(days=7),
    None,
)
INDICE_PERSONALIZADO = len(DURACAO_POR_PERIODO) - 1

FORMATO_DATA_HORA = "%d/%m/%Y %H:%M:%S"
COR_ERRO = "color: #c0392b;"
COR_NORMAL = ""

# (nome na tabela e na legenda, atributo do ponto, atributo da estatística, cor da curva)
SENSORES = (
    ("Temperatura (°C)", "temperatura_c", "temperatura", "#e67e22"),
    ("TDS (ppm)", "tds_ppm", "tds", "#3498db"),
    ("Turbidez (NTU)", "turbidez_ntu", "turbidez", "#1abc9c"),
    ("IQA", "iqa", "iqa", "#f1c40f"),
)


def formatar_duracao(segundos):
    """90 -> '1 min 30 s', 3600 -> '1 h', 86400 -> '24 h'."""
    segundos = int(segundos)
    if segundos < 60:
        return f"{segundos} s"
    if segundos < 3600:
        minutos, resto = divmod(segundos, 60)
        return f"{minutos} min" + (f" {resto} s" if resto else "")
    horas, resto = divmod(segundos, 3600)
    minutos = resto // 60
    return f"{horas} h" + (f" {minutos} min" if minutos else "")


def formato_do_eixo_x(duracao):
    """Formato dos rótulos do eixo X conforme o intervalo desenhado (timedelta).

    Com menos de 1 hora de dados, "HH:mm" repetiria o mesmo rótulo em vários marcadores (6 marcadores em
    poucos minutos): por isso mostra os segundos. Passando de 24 horas, mostra também o dia.
    """
    if duracao < timedelta(hours=1):
        return "HH:mm:ss"
    if duracao <= timedelta(hours=24):
        return "HH:mm"
    return "dd/MM HH:mm"


class HistoricoLeiturasController(QDialog):
    """Janela modal: escolhe um período, consulta o banco (thread do banco) e mostra gráfico + mínimo/média/máximo."""

    def __init__(self, banco, parent=None, relogio=datetime.now):
        super().__init__(parent)
        self.ui = Ui_HistoricoLeiturasDialog()
        self.ui.setupUi(self)

        self._banco = banco          # BancoDeDados: fachada da thread do banco
        self._relogio = relogio      # injetável para testar os períodos "últimas N horas"
        self._consultando = False    # True enquanto uma consulta espera a resposta
        self._ativa = False          # True só com a janela exibida (resposta tardia é descartada)
        self._atualizando_datas = False  # True enquanto o código (não o operador) mexe nos campos De/Até

        self._montar_grafico()

        header = self.ui.table_resumo.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        # A altura da tabela é calculada em showEvent() (depende do estilo); sem rolagem, as 4 linhas cabem.
        self.ui.table_resumo.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.ui.table_resumo.setRowCount(len(SENSORES))
        self._limpar_resumo()

        self._aplicar_periodo_escolhido()  # preenche De/Até com "Última hora"

        self.ui.combo_periodo.currentIndexChanged.connect(self._ao_trocar_periodo)
        self.ui.dt_inicio.dateTimeChanged.connect(self._ao_editar_datas)
        self.ui.dt_fim.dateTimeChanged.connect(self._ao_editar_datas)
        self.ui.btn_consultar.clicked.connect(self.consultar)
        self.ui.buttonBox.rejected.connect(self.reject)
        self.ui.buttonBox.accepted.connect(self.accept)

    # ------------------------------------------------------------------
    # Gráfico
    # ------------------------------------------------------------------
    def _montar_grafico(self):
        self.series = {}
        self.chart = QChart()
        self.chart.setTheme(QChart.ChartThemeDark)
        self.chart.setAnimationOptions(QChart.NoAnimation)
        self.chart.setBackgroundBrush(QColor("#333333"))
        self.chart.setTitleBrush(QColor("#d4d4d4"))
        self.chart.setTitle("Leituras do período")
        self.chart.legend().setVisible(True)
        self.chart.legend().setAlignment(Qt.AlignBottom)
        self.chart.legend().setLabelColor(QColor("#c0c0c0"))

        self.eixo_x = QDateTimeAxis()
        self.eixo_x.setFormat("HH:mm")
        self.eixo_x.setTickCount(6)
        self.eixo_y = QValueAxis()
        self.chart.addAxis(self.eixo_x, Qt.AlignBottom)
        self.chart.addAxis(self.eixo_y, Qt.AlignLeft)

        for nome, atributo, _estatistica, cor in SENSORES:
            serie = QLineSeries()
            serie.setName(nome)
            serie.setColor(QColor(cor))
            self.chart.addSeries(serie)
            serie.attachAxis(self.eixo_x)
            serie.attachAxis(self.eixo_y)
            self.series[atributo] = serie

        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        self.chart_view.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        layout = QVBoxLayout(self.ui.chart_historico)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.chart_view)

    # ------------------------------------------------------------------
    # Janela
    # ------------------------------------------------------------------
    def showEvent(self, event):
        """Ao abrir já consulta o período escolhido (a "Última hora"), sem o operador precisar clicar."""
        super().showEvent(event)
        self._ativa = True
        self._ajustar_altura_da_tabela()
        self.consultar()

    def _ajustar_altura_da_tabela(self):
        """Dá à tabela de resumo a altura exata do cabeçalho + as linhas, como o estilo escuro as desenha.

        Um limite fixo (antes: 150 px) cortava a última linha (IQA) e criava uma barra de rolagem, porque no
        tema escuro cada linha tem mais altura do que no estilo padrão do Qt. Chamado ao exibir a janela,
        quando o estilo já foi aplicado.
        """
        tabela = self.ui.table_resumo
        tabela.resizeRowsToContents()
        altura = (tabela.horizontalHeader().sizeHint().height()
                  + tabela.verticalHeader().length()
                  + 2 * tabela.frameWidth())
        tabela.setFixedHeight(altura)

    def hideEvent(self, event):
        self._ativa = False
        super().hideEvent(event)

    # ------------------------------------------------------------------
    # Período
    # ------------------------------------------------------------------
    def _ao_trocar_periodo(self, _indice):
        self._aplicar_periodo_escolhido()

    def _aplicar_periodo_escolhido(self):
        """Presets recalculam De/Até a partir de AGORA; 'Personalizado' não mexe nos campos."""
        duracao = DURACAO_POR_PERIODO[self.ui.combo_periodo.currentIndex()]
        if duracao is None:
            return
        agora = self._relogio().replace(microsecond=0)
        self._escrever_datas(agora - duracao, agora)

    def _escrever_datas(self, inicio, fim):
        self._atualizando_datas = True
        try:
            self.ui.dt_inicio.setDateTime(self._para_qdatetime(inicio))
            self.ui.dt_fim.setDateTime(self._para_qdatetime(fim))
        finally:
            self._atualizando_datas = False

    def _ao_editar_datas(self, _valor):
        """O operador mexeu em De/Até: vira 'Personalizado' (sem disparar o recálculo do preset)."""
        if self._atualizando_datas:
            return
        if self.ui.combo_periodo.currentIndex() != INDICE_PERSONALIZADO:
            self.ui.combo_periodo.blockSignals(True)
            self.ui.combo_periodo.setCurrentIndex(INDICE_PERSONALIZADO)
            self.ui.combo_periodo.blockSignals(False)

    def _periodo_da_tela(self):
        return (
            self._para_datetime(self.ui.dt_inicio.dateTime()),
            self._para_datetime(self.ui.dt_fim.dateTime()),
        )

    @staticmethod
    def _para_datetime(qdatetime):
        # pelos segundos desde 1970: evita a ambiguidade de fuso de toPython() e bate com o datetime.now() do banco
        return datetime.fromtimestamp(qdatetime.toSecsSinceEpoch())

    @staticmethod
    def _para_qdatetime(momento):
        return QDateTime.fromSecsSinceEpoch(int(momento.timestamp()))

    # ------------------------------------------------------------------
    # Consulta (assíncrona)
    # ------------------------------------------------------------------
    def consultar(self):
        """Pede o período à thread do banco. O gráfico é desenhado em _ao_consulta_concluida()."""
        if self._consultando:
            return  # a anterior ainda não voltou: não empilha pedidos
        self._aplicar_periodo_escolhido()  # preset clicado de novo = "agora" de novo
        inicio, fim = self._periodo_da_tela()
        if inicio >= fim:
            self._mostrar_estado("Período inválido: o início precisa ser anterior ao fim.", erro=True)
            return  # nunca vai ao banco

        self._consultando = True
        self.ui.btn_consultar.setEnabled(False)
        self._mostrar_estado("Consultando o banco...")
        self._banco.executar("consultar_leituras", inicio, fim, MAX_PONTOS_GRAFICO,
                             ao_terminar=self._ao_consulta_concluida)

    def _ao_consulta_concluida(self, resultado):
        """GUI thread, quando a thread do banco terminar."""
        self._consultando = False
        self.ui.btn_consultar.setEnabled(True)
        if not self._ativa:
            return  # a janela fechou enquanto a consulta rodava
        if not resultado.ok:
            logger.error("Falha ao consultar o histórico de leituras: %s", resultado.erro)
            self._limpar_tudo()
            self._mostrar_estado(f"Falha ao consultar o banco: {resultado.erro}", erro=True)
            QMessageBox.critical(self, "Banco de dados", f"Não foi possível carregar as leituras.\n{resultado.erro}")
            return
        self._mostrar(resultado.valor)

    # ------------------------------------------------------------------
    # Resultado
    # ------------------------------------------------------------------
    def _mostrar(self, consulta):
        periodo = f"{consulta.inicio.strftime(FORMATO_DATA_HORA)} até {consulta.fim.strftime(FORMATO_DATA_HORA)}"
        if consulta.total_leituras == 0:
            self._limpar_tudo()
            self._mostrar_estado(
                f"Nenhuma leitura gravada entre {periodo}. "
                "(Só leituras da placa são gravadas: conecte o STM32 e espere alguns segundos.)"
            )
            return

        self._desenhar_curvas(consulta)
        self._preencher_resumo(consulta)

        texto = f"{consulta.total_leituras} leituras entre {periodo}."
        if len(consulta.pontos) < consulta.total_leituras:  # algum ponto resume mais de uma leitura
            texto += (f" Gráfico com {len(consulta.pontos)} pontos, cada um a média de até "
                      f"{formatar_duracao(consulta.segundos_por_ponto)} de leituras; "
                      "mínimo, média e máximo usam todas as leituras.")
        self._mostrar_estado(texto)

    def _desenhar_curvas(self, consulta):
        pontos = consulta.pontos
        for _nome, atributo, _estatistica, _cor in SENSORES:
            self.series[atributo].replace(
                [QPointF(p.timestamp.timestamp() * 1000, getattr(p, atributo)) for p in pontos]
            )

        primeiro, ultimo = pontos[0].timestamp, pontos[-1].timestamp
        if primeiro == ultimo:  # um ponto só: abre uma janelinha para o eixo ter intervalo
            primeiro, ultimo = primeiro - timedelta(minutes=1), ultimo + timedelta(minutes=1)
        self.eixo_x.setFormat(formato_do_eixo_x(ultimo - primeiro))
        self.eixo_x.setRange(self._para_qdatetime(primeiro), self._para_qdatetime(ultimo))

        maior = max(getattr(p, atributo) for p in pontos for _n, atributo, _e, _c in SENSORES)
        menor = min(getattr(p, atributo) for p in pontos for _n, atributo, _e, _c in SENSORES)
        self.eixo_y.setRange(min(0.0, menor * 1.1), maior * 1.1 if maior > 0 else 1.0)

    def _preencher_resumo(self, consulta):
        for linha, (nome, _atributo, estatistica, _cor) in enumerate(SENSORES):
            dados = getattr(consulta, estatistica)
            textos = (nome, f"{dados.minimo:.1f}", f"{dados.media:.1f}", f"{dados.maximo:.1f}")
            for coluna, texto in enumerate(textos):
                item = QTableWidgetItem(texto)
                if coluna > 0:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.ui.table_resumo.setItem(linha, coluna, item)

    def _limpar_resumo(self):
        for linha, (nome, *_resto) in enumerate(SENSORES):
            textos = (nome, "-", "-", "-")
            for coluna, texto in enumerate(textos):
                self.ui.table_resumo.setItem(linha, coluna, QTableWidgetItem(texto))

    def _limpar_tudo(self):
        for serie in self.series.values():
            serie.clear()
        self._limpar_resumo()

    def _mostrar_estado(self, texto, erro=False):
        self.ui.lbl_estado.setText(texto)
        self.ui.lbl_estado.setStyleSheet(COR_ERRO if erro else COR_NORMAL)
