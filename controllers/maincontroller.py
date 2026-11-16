"""
Controller da janela principal.

Concentra a lógica: leitura simulada dos sensores, gráficos de tendência,
checagem de limite (setpoint), estado da válvula, corte emergencial e
histórico de eventos. A tela (ui/tela.ui + Ui_tela.py) não tem nenhuma
regra de negócio, só o desenho.
"""
import logging
import sqlite3
from datetime import datetime, timedelta

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, QPointF, QThread, QTimer, QUrl, Qt
from PySide6.QtGui import QAction, QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import QGraphicsView, QMainWindow, QMessageBox, QVBoxLayout

from controllers.historicocontroller import HistoricoController
from controllers.settingscontroller import SettingsController
from models.config_manager import ConfigManager
from models.data_simulator import SensorSimulator
from models.dispositivo_repository import TAG_RELE_EMERGENCIA, TAG_VALVULA, DispositivoRepository
from models.evento_repository import EventoRepository
from models.regra_repository import RegraRepository
from models.logger import ARQUIVO_LOG
from models.serial_worker import BAUD_PADRAO, MOTIVO_OPERADOR, SerialWorker
from models.sensor_data import EstadoValvula, RegistroEvento, RegraAlerta, StatusDispositivo, TipoEvento
from ui.Ui_tela import Ui_MainWindow

HORAS_GRAFICO_DETALHE = 1
HORAS_GRAFICO_GERAL = 24
PROBABILIDADE_TRIP = 0.01
OPCAO_AUTOMATICO = "Automático"  # o worker acha o STM32 sozinho

# cores do texto de status da conexão serial
COR_CONECTADO = "#27ae60"
COR_PROCURANDO = "#e0a132"
COR_DESCONECTADO = "#c0392b"


logger = logging.getLogger(__name__)

# severidade usada no smartgrid.log para cada tipo de evento da tabela
NIVEL_LOG_POR_TIPO = {
    TipoEvento.STATUS: logging.INFO,
    TipoEvento.COMANDO: logging.INFO,
    TipoEvento.ALERTA: logging.WARNING,
}

# sensor da regra -> (atributo da LeituraTelemetria, nome curto, unidade)
SENSORES_LEITURA = {
    "TDS (ppm)": ("tds_ppm", "TDS", "ppm"),
    "Turbidez (NTU)": ("turbidez_ntu", "Turbidez", "NTU"),
    "Temperatura (°C)": ("temperatura_c", "Temperatura", "°C"),
}


class MainController(QMainWindow):
    def __init__(self):
        super().__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        # parametros de inicializacao/comunicacao (data/config.json)
        self.config = ConfigManager()

        self.simulador = SensorSimulator()
        self.estado_valvula = EstadoValvula.ABERTA
        self.id_valvula = None  # id da VALV-01 na tabela dispositivos (FK dos eventos)

        # leitura serial: QThread + Worker (padrão da aula de 26/10). As referências ficam em
        # atributos para o coletor de lixo não destruir os objetos enquanto a thread roda
        self.thread_serial = None
        self.worker_serial = None
        self.modo_real = False  # False = dados simulados; True = dados vindos do STM32


        # cada lista guarda o historico completo (tempo + valor) de um sensor
        self.tempos = []
        self.temperaturas = []
        self.tds_valores = []
        self.turbidez_valores = []

        # regras de alerta persistidas no SQLite (tabela regras_alerta)
        self.regras = self._carregar_regras()
        self.regras_violadas = set()  # regras (nome, sensor) estouradas na ultima leitura
        self._sincronizar_spinboxes()

        self._montar_graficos()
        self._carregar_historico_inicial()
        self._conectar_sinais()
        self._montar_menu_diagnostico()
        self._aplicar_config_serial()
        self._carregar_estado_dispositivos()

        self._log_evento(TipoEvento.STATUS, "Dashboard inicializado (dados simulados).", "-")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.atualizar_telemetria)
        self.timer.start(self._intervalo_atualizacao_ms())

        # assim que a janela aparece, procura a porta do STM32 sozinho (modo Automático)
        QTimer.singleShot(0, self.iniciar_leitura_serial)


    # ------------------------------------------------------------------
    # Regras de alerta (SQLite)
    # ------------------------------------------------------------------
    def _regras_padrao(self):
        """Regras iniciais montadas a partir de limites_iniciais do config.json."""
        limites = self.config.obter("limites_iniciais")
        try:
            tds = float(limites["tds_max_ppm"])
            turbidez = float(limites["turbidez_max_ntu"])
        except (KeyError, TypeError, ValueError):
            logger.warning("limites_iniciais inválidos no config.json; usando os valores padrão do programa.")
            padrao = ConfigManager.DEFAULT_CONFIG["limites_iniciais"]
            tds, turbidez = padrao["tds_max_ppm"], padrao["turbidez_max_ntu"]
        return [
            RegraAlerta(nome="TDS máximo", sensor="TDS (ppm)", valor_maximo=tds, ativa=True),
            RegraAlerta(nome="Turbidez máxima", sensor="Turbidez (NTU)", valor_maximo=turbidez, ativa=True),
        ]

    def _carregar_regras(self):
        try:
            regras = RegraRepository.listar_todos()
            if not regras:  # primeira execucao: banco vazio
                regras = self._regras_padrao()
                RegraRepository.substituir_todas(regras)
                logger.info("Tabela regras_alerta vazia: regras iniciais criadas a partir do config.json.")
            return regras
        except sqlite3.Error as erro:
            logger.error("Falha ao carregar as regras do SQLite: %s. Usando regras padrão em memória.", erro)
            QMessageBox.warning(self, "Banco de dados", f"Não foi possível carregar as regras salvas.\n{erro}")
            return self._regras_padrao()

    def _primeira_regra_ativa(self, sensor):
        return next((r for r in self.regras if r.sensor == sensor and r.ativa), None)

    def _sincronizar_spinboxes(self):
        """Os spinboxes do dashboard mostram (e editam) a 1a regra ativa de TDS e de Turbidez."""
        for sensor, spin in (("TDS (ppm)", self.ui.spin_limite_tds), ("Turbidez (NTU)", self.ui.spin_limite_turbidez)):
            regra = self._primeira_regra_ativa(sensor)
            spin.setEnabled(regra is not None)
            if regra is not None:
                spin.setValue(regra.valor_maximo)

    def _alterar_limite_rapido(self, sensor, spin):
        """Chamado quando o operador termina de editar um spinbox de limite no dashboard."""
        regra = self._primeira_regra_ativa(sensor)
        novo_valor = round(spin.value(), 2)
        if regra is None or abs(regra.valor_maximo - novo_valor) < 1e-9:
            return  # nada mudou

        if novo_valor <= 0:
            logger.warning("Entrada inválida bloqueada: limite de %s = %s (deve ser maior que zero).", sensor, novo_valor)
            QMessageBox.warning(self, "Entrada inválida", "O limite deve ser maior que zero.")
            spin.setValue(regra.valor_maximo)
            return

        valor_anterior = regra.valor_maximo
        regra.valor_maximo = novo_valor
        try:
            if regra.id is None or not RegraRepository.atualizar(regra):
                raise LookupError("regra não encontrada no banco")
        except (sqlite3.Error, LookupError) as erro:
            regra.valor_maximo = valor_anterior
            spin.setValue(valor_anterior)
            logger.error("Falha ao salvar o limite de %s no SQLite: %s", sensor, erro)
            QMessageBox.critical(self, "Falha ao salvar", f"O limite não foi salvo.\n{erro}")
            return

        self._log_evento(
            TipoEvento.COMANDO,
            f"Limite de {sensor} alterado de {valor_anterior:.2f} para {novo_valor:.2f}.",
            f"{novo_valor:.2f}",
        )

    def _intervalo_atualizacao_ms(self):
        try:
            intervalo = int(self.config.obter("intervalo_atualizacao_ms"))
        except (TypeError, ValueError):
            intervalo = ConfigManager.DEFAULT_CONFIG["intervalo_atualizacao_ms"]
            logger.warning("intervalo_atualizacao_ms inválido no config.json; usando %s ms.", intervalo)
        return max(200, intervalo)  # evita travar a tela com um valor absurdo

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _criar_mini_grafico(self, titulo):
        serie = QLineSeries()

        chart = QChart()
        chart.setTheme(QChart.ChartThemeDark)
        chart.setAnimationOptions(QChart.NoAnimation)
        chart.setBackgroundBrush(QColor("#333333"))
        chart.setTitleBrush(QColor("#d4d4d4"))
        chart.setTitle(titulo)
        chart.addSeries(serie)
        chart.legend().setVisible(False)

        eixo_x = QDateTimeAxis()
        eixo_x.setFormat("HH:mm")
        eixo_x.setTickCount(6)
        eixo_y = QValueAxis()

        chart.addAxis(eixo_x, Qt.AlignBottom)
        chart.addAxis(eixo_y, Qt.AlignLeft)
        serie.attachAxis(eixo_x)
        serie.attachAxis(eixo_y)

        return chart, serie, eixo_x, eixo_y

    def _exibir_grafico(self, container, chart):
        chart_view = QChartView(chart)
        chart_view.setRenderHint(QPainter.Antialiasing)
        # forca redesenho completo do viewport a cada atualizacao: sem isso, em alguns
        # ambientes Windows o QChartView so repinta a regiao "suja" e a linha antiga
        # some/fica parcial quando os pontos mudam rapido (a cada 2s).
        chart_view.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(chart_view)
        return chart_view

    def _montar_graficos(self):
        self.chart_temp, self.serie_temperatura, self.eixo_x_temp, self.eixo_y_temp = self._criar_mini_grafico(
            "Temperatura (°C) — última 1h"
        )
        self.chart_tds, self.serie_tds, self.eixo_x_tds, self.eixo_y_tds = self._criar_mini_grafico(
            "TDS (ppm) — última 1h"
        )
        self.chart_turbidez, self.serie_turbidez, self.eixo_x_turbidez, self.eixo_y_turbidez = (
            self._criar_mini_grafico("Turbidez (NTU) — última 1h")
        )

        self.view_temp = self._exibir_grafico(self.ui.chart_temperatura, self.chart_temp)
        self.view_tds = self._exibir_grafico(self.ui.chart_tds, self.chart_tds)
        self.view_turbidez = self._exibir_grafico(self.ui.chart_turbidez, self.chart_turbidez)

        # grafico geral, com os 3 sensores juntos e as 24h completas
        self.serie_geral_temperatura = QLineSeries()
        self.serie_geral_temperatura.setName("Temperatura (°C)")
        self.serie_geral_tds = QLineSeries()
        self.serie_geral_tds.setName("TDS (ppm)")
        self.serie_geral_turbidez = QLineSeries()
        self.serie_geral_turbidez.setName("Turbidez (NTU)")

        self.chart_geral = QChart()
        self.chart_geral.setTheme(QChart.ChartThemeDark)
        self.chart_geral.setAnimationOptions(QChart.NoAnimation)
        self.chart_geral.setBackgroundBrush(QColor("#333333"))
        self.chart_geral.setTitleBrush(QColor("#d4d4d4"))
        self.chart_geral.setTitle("Visão Geral — últimas 24h")
        self.chart_geral.addSeries(self.serie_geral_temperatura)
        self.chart_geral.addSeries(self.serie_geral_tds)
        self.chart_geral.addSeries(self.serie_geral_turbidez)
        self.chart_geral.legend().setVisible(True)
        self.chart_geral.legend().setAlignment(Qt.AlignBottom)
        self.chart_geral.legend().setLabelColor(QColor("#c0c0c0"))

        self.eixo_x_geral = QDateTimeAxis()
        self.eixo_x_geral.setFormat("HH:mm")
        self.eixo_x_geral.setTickCount(6)
        self.eixo_y_geral = QValueAxis()
        self.chart_geral.addAxis(self.eixo_x_geral, Qt.AlignBottom)
        self.chart_geral.addAxis(self.eixo_y_geral, Qt.AlignLeft)
        for serie in (self.serie_geral_temperatura, self.serie_geral_tds, self.serie_geral_turbidez):
            serie.attachAxis(self.eixo_x_geral)
            serie.attachAxis(self.eixo_y_geral)

        self.view_geral = self._exibir_grafico(self.ui.chart_geral, self.chart_geral)

    def _carregar_historico_inicial(self):
        historico = self.simulador.gerar_historico(horas=24, intervalo_min=10)
        for leitura in historico:
            self.tempos.append(leitura.timestamp)
            self.temperaturas.append(leitura.temperatura_c)
            self.tds_valores.append(leitura.tds_ppm)
            self.turbidez_valores.append(leitura.turbidez_ntu)

        self._atualizar_lcds(historico[-1])
        self._redesenhar_graficos()

    def _conectar_sinais(self):
        self.ui.btn_emergencia.clicked.connect(self.acionar_corte_emergencial)

        # ---> ATALHO DE TECLADO ADICIONADO AQUI <---
        self.ui.btn_emergencia.setShortcut("Space")

        self.ui.btn_abrir_valvula.clicked.connect(self.reabrir_valvula)
        self.ui.btn_conectar.clicked.connect(self.conectar_serial)
        self.ui.btn_desconectar.clicked.connect(self.desconectar_serial)
        self.ui.btn_abrir_configuracoes.clicked.connect(self.abrir_configuracoes)
        self.ui.btn_abrir_historico.clicked.connect(self.abrir_historico)
        self.ui.spin_limite_tds.editingFinished.connect(
            lambda: self._alterar_limite_rapido("TDS (ppm)", self.ui.spin_limite_tds)
        )
        self.ui.spin_limite_turbidez.editingFinished.connect(
            lambda: self._alterar_limite_rapido("Turbidez (NTU)", self.ui.spin_limite_turbidez)
        )

    def abrir_configuracoes(self):
        dialogo = SettingsController(self.regras, parent=self)
        if not dialogo.exec():
            return  # operador cancelou

        try:
            RegraRepository.substituir_todas(dialogo.regras)  # transacao unica: tudo ou nada
            self.regras = RegraRepository.listar_todos()
        except sqlite3.Error as erro:
            logger.error("Falha ao salvar as regras no SQLite. Rollback executado: %s", erro)
            QMessageBox.critical(
                self, "Falha ao salvar", f"As regras NÃO foram salvas (as anteriores foram mantidas).\n{erro}"
            )
            return

        self.regras_violadas.clear()
        self._sincronizar_spinboxes()
        self._log_evento(
            TipoEvento.COMANDO, f"Regras de alerta atualizadas via configuração ({len(self.regras)} regras).", "-"
        )

    def abrir_historico(self):
        dialogo = HistoricoController(parent=self)
        dialogo.exec()

    # ------------------------------------------------------------------
    # Loop de telemetria
    # ------------------------------------------------------------------
    def atualizar_telemetria(self):
        """Tique do simulador (só roda enquanto não há STM32 conectado)."""
        leitura = self.simulador.proxima_leitura()
        self._processar_leitura(leitura)
        self._checar_protecao_espontanea(leitura)

    def _processar_leitura(self, leitura):
        """Caminho único de uma leitura (simulada ou vinda do STM32): listas, tela, gráficos e limites."""
        self.tempos.append(leitura.timestamp)
        self.temperaturas.append(leitura.temperatura_c)
        self.tds_valores.append(leitura.tds_ppm)
        self.turbidez_valores.append(leitura.turbidez_ntu)

        self._atualizar_lcds(leitura)
        self._redesenhar_graficos()
        self._checar_setpoints(leitura)

    def _atualizar_lcds(self, leitura):
        self.ui.lcd_temperatura.display(leitura.temperatura_c)
        self.ui.lcd_tds.display(leitura.tds_ppm)
        self.ui.lcd_turbidez.display(leitura.turbidez_ntu)
        self.ui.lcd_iqa.display(leitura.iqa)

    # ------------------------------------------------------------------
    # Graficos
    # ------------------------------------------------------------------
    def _pontos_na_janela(self, valores, horas):
        if not self.tempos:
            return []
        limite = self.tempos[-1] - timedelta(hours=horas)
        return [(t, v) for t, v in zip(self.tempos, valores) if t >= limite]

    def _atualizar_grafico_detalhe(self, serie, valores, eixo_x, eixo_y):
        pares = self._pontos_na_janela(valores, HORAS_GRAFICO_DETALHE)
        if not pares:
            return
        serie.replace([QPointF(t.timestamp() * 1000, v) for t, v in pares])
        tempos = [t for t, _ in pares]
        vals = [v for _, v in pares]
        if tempos[0] == tempos[-1]:
            return  # só 1 leitura até agora (começo da leitura real): ainda não há intervalo de tempo
        eixo_x.setRange(
            QDateTime.fromMSecsSinceEpoch(int(tempos[0].timestamp() * 1000)),
            QDateTime.fromMSecsSinceEpoch(int(tempos[-1].timestamp() * 1000)),
        )
        baixo, alto = min(vals) * 0.9, max(vals) * 1.1
        if baixo == alto:  # valor constante igual a 0
            baixo, alto = baixo - 1, alto + 1
        eixo_y.setRange(baixo, alto)

    def _redesenhar_graficos(self):
        self._atualizar_grafico_detalhe(self.serie_temperatura, self.temperaturas, self.eixo_x_temp, self.eixo_y_temp)
        self._atualizar_grafico_detalhe(self.serie_tds, self.tds_valores, self.eixo_x_tds, self.eixo_y_tds)
        self._atualizar_grafico_detalhe(
            self.serie_turbidez, self.turbidez_valores, self.eixo_x_turbidez, self.eixo_y_turbidez
        )

        pares_temp = self._pontos_na_janela(self.temperaturas, HORAS_GRAFICO_GERAL)
        pares_tds = self._pontos_na_janela(self.tds_valores, HORAS_GRAFICO_GERAL)
        pares_turbidez = self._pontos_na_janela(self.turbidez_valores, HORAS_GRAFICO_GERAL)
        if not pares_temp:
            return

        self.serie_geral_temperatura.replace([QPointF(t.timestamp() * 1000, v) for t, v in pares_temp])
        self.serie_geral_tds.replace([QPointF(t.timestamp() * 1000, v) for t, v in pares_tds])
        self.serie_geral_turbidez.replace([QPointF(t.timestamp() * 1000, v) for t, v in pares_turbidez])

        tempos = [t for t, _ in pares_temp]
        if tempos[0] == tempos[-1]:
            return  # só 1 leitura até agora: ainda não há intervalo de tempo
        self.eixo_x_geral.setRange(
            QDateTime.fromMSecsSinceEpoch(int(tempos[0].timestamp() * 1000)),
            QDateTime.fromMSecsSinceEpoch(int(tempos[-1].timestamp() * 1000)),
        )
        maior = max(max(v for _, v in pares_tds), max(v for _, v in pares_turbidez), max(v for _, v in pares_temp))
        self.eixo_y_geral.setRange(0, maior * 1.1)

        for view in (self.view_temp, self.view_tds, self.view_turbidez, self.view_geral):
            view.scene().invalidate(view.scene().sceneRect())
            view.viewport().repaint()

    # ------------------------------------------------------------------
    # Setpoints
    # ------------------------------------------------------------------
    def _checar_setpoints(self, leitura):
        """Compara a leitura com TODAS as regras ativas do banco (TDS, Turbidez e Temperatura)."""
        violadas_agora = set()
        for regra in self.regras:
            if not regra.ativa or regra.sensor not in SENSORES_LEITURA:
                continue
            atributo, nome, unidade = SENSORES_LEITURA[regra.sensor]
            valor = getattr(leitura, atributo)
            if valor <= regra.valor_maximo:
                continue

            chave = (regra.nome, regra.sensor)
            violadas_agora.add(chave)
            if chave not in self.regras_violadas:  # registra so quando a violacao comeca
                descricao = (
                    f"Limite de {nome} violado: {valor:.2f} {unidade} "
                    f"(limite configurado: {regra.valor_maximo:.2f} {unidade})."
                )
                self._log_evento(TipoEvento.ALERTA, descricao, f"{valor:.2f} {unidade}")
                self._aplicar_alarme_visual(True, descricao)

        if self.regras_violadas and not violadas_agora:
            self._aplicar_alarme_visual(False, "Leituras normalizadas.")
            self._log_evento(TipoEvento.STATUS, "Leituras normalizadas (todos os limites respeitados).", "-")
        self.regras_violadas = violadas_agora

    def _aplicar_alarme_visual(self, ativo, texto):
        """Dashboard fica com borda amarela enquanto algum limite estiver violado."""
        if ativo:
            self.ui.group_telemetria.setStyleSheet("QGroupBox { border: 2px solid #e0a132; }")
            self.ui.lbl_ultimo_evento.setStyleSheet("color: #e0a132; font-weight: bold;")
        else:
            self.ui.group_telemetria.setStyleSheet("")
            self.ui.lbl_ultimo_evento.setStyleSheet("")
        self.ui.lbl_ultimo_evento.setText(texto)

    # ------------------------------------------------------------------
    # Valvula (estado persistido na tabela dispositivos)
    # ------------------------------------------------------------------
    def _carregar_estado_dispositivos(self):
        """Restaura o estado salvo no banco: se o app fechou com a valvula fechada, ela abre fechada."""
        try:
            valvula = DispositivoRepository.buscar_por_tag(TAG_VALVULA)
        except sqlite3.Error as erro:
            logger.error("Falha ao ler o estado da válvula no SQLite: %s. Assumindo ABERTA.", erro)
            valvula = None

        if valvula is None:
            self._aplicar_estado_valvula(EstadoValvula.ABERTA)
            return

        self.id_valvula = valvula.id
        estado = EstadoValvula.ABERTA if valvula.status == StatusDispositivo.ABERTO else EstadoValvula.FECHADA
        self._aplicar_estado_valvula(estado)
        logger.info("Estado restaurado do banco: %s = %s.", valvula.tag, valvula.status.value)

    def _aplicar_estado_valvula(self, estado):
        """So atualiza a TELA (badge + botoes). Quem grava no banco e o _mudar_estado_valvula."""
        self.estado_valvula = estado
        cor = "#2ecc71" if estado == EstadoValvula.ABERTA else "#c0392b"
        self.ui.lbl_valvula_status.setText(f"Válvula: {estado.value}")
        self.ui.lbl_valvula_status.setStyleSheet(
            f"background-color: {cor}; color: white; font-weight: bold; border-radius: 4px; padding: 8px;"
        )
        self._atualizar_botoes_valvula()

    def _resumo_ultima_leitura(self):
        if not self.tds_valores:
            return "-"
        return f"TDS {self.tds_valores[-1]:.1f} ppm / Turbidez {self.turbidez_valores[-1]:.2f} NTU"

    def _mudar_estado_valvula(self, estado_destino, tipo_evento, motivo, valor, simular_falha=False):
        """
        Muda o estado fisico (valvula + rele) e grava o evento numa UNICA transacao.
        Devolve True se deu certo. Se falhar, o banco faz ROLLBACK e a tela continua
        mostrando o estado anterior, que e o que realmente esta gravado.
        """
        fechando = estado_destino == EstadoValvula.FECHADA
        if fechando:
            # corte: valvula FECHA e o rele de emergencia ABRE o circuito da solenoide
            novos_status = {TAG_VALVULA: StatusDispositivo.FECHADO, TAG_RELE_EMERGENCIA: StatusDispositivo.ABERTO}
        else:
            novos_status = {TAG_VALVULA: StatusDispositivo.ABERTO, TAG_RELE_EMERGENCIA: StatusDispositivo.FECHADO}

        evento = RegistroEvento(
            timestamp=datetime.now(),
            tipo=tipo_evento,
            descricao=motivo,
            valor_medido=valor,
            dispositivo_id=self.id_valvula,
        )

        try:
            DispositivoRepository.alterar_estados_com_evento(novos_status, evento, simular_falha=simular_falha)
        except (sqlite3.Error, RuntimeError, LookupError) as erro:
            operacao = "corte emergencial" if fechando else "reabertura da válvula"
            logger.error("Falha na operação de %s. Rollback executado: %s", operacao, erro)
            self._registrar_estado_pos_rollback()
            QMessageBox.critical(
                self,
                "Falha na operação",
                f"A operação de {operacao} falhou e foi desfeita (ROLLBACK).\n"
                f"Nenhuma alteração foi gravada no banco.\n\nMotivo: {erro}",
            )
            return False

        self._aplicar_estado_valvula(estado_destino)
        self.ui.lbl_ultimo_evento.setStyleSheet("")
        self.ui.lbl_ultimo_evento.setText(motivo)
        if fechando:
            logger.warning(
                "Corte de carga executado: %s FECHADO / %s ABERTO. Motivo: %s (valor medido: %s)",
                TAG_VALVULA, TAG_RELE_EMERGENCIA, motivo, valor,
            )
        else:
            logger.info("Válvula reaberta: %s ABERTO / %s FECHADO. (valor medido: %s)",
                        TAG_VALVULA, TAG_RELE_EMERGENCIA, valor)
        return True

    def _registrar_estado_pos_rollback(self):
        """Le o banco depois da falha e escreve no log o estado que ficou gravado (prova do rollback)."""
        # a tela nao foi alterada antes da falha, entao ela ja mostra o estado correto
        try:
            estados = ", ".join(f"{d.tag}={d.status.value}" for d in DispositivoRepository.listar_todos())
            logger.info("Estado no banco após o rollback: %s.", estados)
        except sqlite3.Error as erro:
            logger.error("Não foi possível ler o estado após o rollback: %s", erro)

    def _checar_protecao_espontanea(self, leitura):
        if self.estado_valvula == EstadoValvula.ABERTA and self.simulador.deveria_disparar_protecao(
            PROBABILIDADE_TRIP
        ):
            self._fechar_valvula(
                "Disparo espontâneo detectado no hardware (simulado).",
                f"TDS {leitura.tds_ppm:.1f} ppm / Turbidez {leitura.turbidez_ntu:.2f} NTU",
                avisar=True,
            )

    def _fechar_valvula(self, motivo, valor, avisar, simular_falha=False):
        tipo = TipoEvento.ALERTA if avisar else TipoEvento.COMANDO
        if self._mudar_estado_valvula(EstadoValvula.FECHADA, tipo, motivo, valor, simular_falha) and avisar:
            QMessageBox.warning(self, "Proteção Ativada", motivo)

    def acionar_corte_emergencial(self):
        resposta = QMessageBox.question(
            self,
            "Confirmar Corte Emergencial",
            "Tem certeza que deseja fechar a válvula de entrada imediatamente?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resposta != QMessageBox.Yes:
            return

        simular = self.acao_simular_falha.isChecked()
        self._fechar_valvula(
            "Corte de emergência acionado via software.",
            self._resumo_ultima_leitura(),
            avisar=False,
            simular_falha=simular,
        )
        if simular:
            self.acao_simular_falha.setChecked(False)  # a falha simulada vale para um unico corte

    def reabrir_valvula(self):
        resposta = QMessageBox.question(
            self,
            "Confirmar Reabertura",
            "Confirma a reabertura da válvula de entrada?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resposta != QMessageBox.Yes:
            return
        self._mudar_estado_valvula(
            EstadoValvula.ABERTA,
            TipoEvento.COMANDO,
            "Válvula reaberta manualmente via software.",
            self._resumo_ultima_leitura(),
        )

    def _atualizar_botoes_valvula(self):
        aberta = self.estado_valvula == EstadoValvula.ABERTA
        self.ui.btn_emergencia.setEnabled(aberta)
        self.ui.btn_abrir_valvula.setEnabled(not aberta)

    # ------------------------------------------------------------------
    # Menu Diagnostico (demonstracao de falhas na arguicao)
    # ------------------------------------------------------------------
    def _montar_menu_diagnostico(self):
        menu = self.ui.menubar.addMenu("&Diagnóstico")

        self.acao_simular_falha = QAction("Simular falha no próximo corte emergencial", self)
        self.acao_simular_falha.setCheckable(True)
        self.acao_simular_falha.setShortcut("Ctrl+Shift+F")
        self.acao_simular_falha.toggled.connect(self._ao_alternar_simulacao_falha)
        menu.addAction(self.acao_simular_falha)

        menu.addSeparator()

        acao_abrir_log = QAction("Abrir arquivo de log (smartgrid.log)", self)
        acao_abrir_log.setShortcut("Ctrl+L")
        acao_abrir_log.triggered.connect(self._abrir_arquivo_log)
        menu.addAction(acao_abrir_log)

    def _ao_alternar_simulacao_falha(self, ativo):
        if ativo:
            logger.warning(
                "MODO DE TESTE ATIVADO: o próximo corte emergencial vai falhar de propósito "
                "entre a atualização dos dispositivos e o registro no histórico."
            )
            self.statusBar().showMessage(
                "MODO DE TESTE: o próximo corte emergencial vai falhar de propósito (Ctrl+Shift+F desliga)."
            )
        else:
            logger.info("Modo de teste de falha desativado.")
            self.statusBar().clearMessage()

    def _abrir_arquivo_log(self, _checked=False):
        if not ARQUIVO_LOG.exists():
            QMessageBox.information(self, "Log", "O arquivo smartgrid.log ainda não foi criado.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ARQUIVO_LOG)))

    # ------------------------------------------------------------------
    # Painel serial e leitura real (QThread + Worker)
    # ------------------------------------------------------------------
    def _aplicar_config_serial(self):
        """Preenche o painel serial com os valores salvos no config.json."""
        serial = self.config.obter("serial")
        for combo, valor in (
            (self.ui.combo_porta_com, str(serial["porta"])),
            (self.ui.combo_baud_rate, str(serial["baud_rate"])),
        ):
            if combo.findText(valor) < 0:  # porta/baud que nao esta na lista padrao
                combo.addItem(valor)
            combo.setCurrentText(valor)

        # 1ª opção do combo: "Automático" varre as portas e acha o STM32 sozinho.
        # A porta salva no config.json continua na lista (para quem quiser fixar uma COM).
        self.ui.combo_porta_com.insertItem(0, OPCAO_AUTOMATICO)
        self.ui.combo_porta_com.setCurrentIndex(0)

        try:
            self.ui.spin_timeout.setValue(int(serial["timeout_s"]))
        except (TypeError, ValueError):
            logger.warning("timeout_s inválido no config.json: %r. Mantido o valor da tela.", serial["timeout_s"])

    def conectar_serial(self):
        """Botão Conectar: "Automático" varre as portas; uma COM escolhida no combo fica fixa."""
        self.iniciar_leitura_serial()

    def desconectar_serial(self):
        """Botão Desconectar: interrupção cooperativa; a QThread termina sozinha em instantes."""
        self.parar_leitura_serial()

    def iniciar_leitura_serial(self):
        """Cria a QThread + SerialWorker (padrão da aula de 26/10) e começa a procurar o STM32."""
        if self.thread_serial is not None:
            self.statusBar().showMessage("A leitura serial já está em andamento (ou encerrando).", 4000)
            return

        escolha = self.ui.combo_porta_com.currentText()
        porta_fixa = None if escolha == OPCAO_AUTOMATICO else escolha
        try:
            baud = int(self.ui.combo_baud_rate.currentText())
        except ValueError:
            baud = BAUD_PADRAO

        self.thread_serial = QThread()
        self.worker_serial = SerialWorker(
            baud_rate=baud,
            porta_fixa=porta_fixa,
            timeout_sem_dados_s=self.ui.spin_timeout.value(),
        )
        self.worker_serial.moveToThread(self.thread_serial)

        # o start da thread dispara o laço do Worker
        self.thread_serial.started.connect(self.worker_serial.executar)

        # Worker -> interface (estes slots rodam na GUI thread)
        self.worker_serial.leitura_recebida.connect(self._processar_leitura)
        self.worker_serial.conectado.connect(self._ao_conectar_serial)
        self.worker_serial.desconectado.connect(self._ao_desconectar_serial)
        self.worker_serial.status_msg.connect(self._ao_status_serial)
        self.worker_serial.portas_atualizadas.connect(self._atualizar_combo_portas)
        self.worker_serial.aviso.connect(self._ao_aviso_serial)
        self.worker_serial.finalizado.connect(self._ao_finalizar_serial)

        # ciclo de vida e liberação de memória (mesmo padrão da aula)
        self.worker_serial.finalizado.connect(self.thread_serial.quit)
        self.worker_serial.finalizado.connect(self.worker_serial.deleteLater)
        self.thread_serial.finished.connect(self.thread_serial.deleteLater)
        self.thread_serial.finished.connect(self._limpar_referencias_serial)

        self._definir_status_conexao("Status: Procurando dispositivo...", COR_PROCURANDO)
        self.thread_serial.start()

    def parar_leitura_serial(self):
        """Interrupção cooperativa: o worker só levanta a bandeira; nada de terminate()."""
        if self.worker_serial is not None:
            try:
                self.worker_serial.parar()
            except RuntimeError:
                pass  # o worker já foi destruído pelo deleteLater

    def _limpar_referencias_serial(self):
        self.thread_serial = None
        self.worker_serial = None

    def _definir_status_conexao(self, texto, cor):
        self.ui.lbl_status_conexao.setText(texto)
        self.ui.lbl_status_conexao.setStyleSheet(f"font-weight: bold; color: {cor};")

    def _atualizar_combo_portas(self, portas):
        """Mantém o combo com as portas que o sistema está enxergando (sem mexer na escolha atual)."""
        combo = self.ui.combo_porta_com
        for porta in portas:
            if combo.findText(porta) < 0:
                combo.addItem(porta)

    def _ao_status_serial(self, texto):
        self._definir_status_conexao(f"Status: {texto}", COR_PROCURANDO)

    def _ao_conectar_serial(self, porta, baud):
        self._entrar_no_modo_real()
        self._definir_status_conexao(f"Status: Conectado ({porta} @ {baud} bps)", COR_CONECTADO)
        # a porta que realmente funcionou fica salva no config.json
        if not self.config.atualizar_serial(porta, baud, self.ui.spin_timeout.value()):
            self.statusBar().showMessage("Não foi possível salvar os parâmetros em config.json.", 6000)
        self._log_evento(TipoEvento.STATUS, f"Conexão serial estabelecida — {porta}.", f"{baud} bps")

    def _ao_desconectar_serial(self, motivo):
        self._definir_status_conexao(f"Status: Desconectado — {motivo}", COR_DESCONECTADO)
        if motivo == MOTIVO_OPERADOR:
            self._log_evento(TipoEvento.STATUS, "Conexão serial encerrada pelo operador.", "-")
        else:
            self._log_evento(TipoEvento.ALERTA, f"Conexão serial perdida: {motivo}.", "-")

    def _ao_aviso_serial(self, texto, eh_problema):
        self.statusBar().showMessage(texto, 8000)
        self._log_evento(TipoEvento.ALERTA if eh_problema else TipoEvento.STATUS, texto, "-")

    def _ao_finalizar_serial(self):
        self._definir_status_conexao("Status: Desconectado", COR_DESCONECTADO)

    def _entrar_no_modo_real(self):
        """Primeira conexão: desliga o simulador e limpa o histórico falso. Reconexões mantêm os dados."""
        if self.modo_real:
            return
        self.timer.stop()
        for lista in (self.tempos, self.temperaturas, self.tds_valores, self.turbidez_valores):
            lista.clear()
        for serie in (
            self.serie_temperatura, self.serie_tds, self.serie_turbidez,
            self.serie_geral_temperatura, self.serie_geral_tds, self.serie_geral_turbidez,
        ):
            serie.clear()
        for lcd in (self.ui.lcd_temperatura, self.ui.lcd_tds, self.ui.lcd_turbidez, self.ui.lcd_iqa):
            lcd.display(0)
        self.modo_real = True
        self._log_evento(TipoEvento.STATUS, "Leitura real ativada: simulador desligado.", "-")

    def closeEvent(self, event):
        """Fechar a janela: pede para o worker parar e espera a QThread acabar (aula de 26/10)."""
        self.parar_leitura_serial()
        if self.thread_serial is not None:
            try:
                self.thread_serial.quit()
                self.thread_serial.wait(5000)
            except RuntimeError:
                pass  # a thread já foi destruída pelo deleteLater
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Historico
    # ------------------------------------------------------------------
    def _log_evento(self, tipo, descricao, valor, nivel_log=None):
        """Registra o evento no SQLite (tabela eventos) e no logs/smartgrid.log."""
        registro = RegistroEvento(timestamp=datetime.now(), tipo=tipo, descricao=descricao, valor_medido=valor)

        nivel = nivel_log if nivel_log is not None else NIVEL_LOG_POR_TIPO[tipo]
        logger.log(nivel, "%s: %s (valor medido: %s)", tipo.value, descricao, valor)

        try:
            EventoRepository.inserir(registro)
        except sqlite3.Error as erro:
            # o evento continua no smartgrid.log mesmo se o banco falhar
            logger.error("Falha ao gravar evento no SQLite (%s): %s", descricao, erro)
            self.statusBar().showMessage(f"ERRO: evento não gravado no banco — {erro}", 10000)
        return registro