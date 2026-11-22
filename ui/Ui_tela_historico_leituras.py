# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'tela_historico_leituras.ui'
##
## Created by: Qt User Interface Compiler version 6.11.1
##
## WARNING! All changes made in this file will be lost when recompiling UI file!
################################################################################

from PySide6.QtCore import (QCoreApplication, QDate, QDateTime, QLocale,
    QMetaObject, QObject, QPoint, QRect,
    QSize, QTime, QUrl, Qt)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QCursor,
    QFont, QFontDatabase, QGradient, QIcon,
    QImage, QKeySequence, QLinearGradient, QPainter,
    QPalette, QPixmap, QRadialGradient, QTransform)
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QApplication, QComboBox,
    QDateTimeEdit, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QSizePolicy,
    QSpacerItem, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget)

class Ui_HistoricoLeiturasDialog(object):
    def setupUi(self, HistoricoLeiturasDialog):
        if not HistoricoLeiturasDialog.objectName():
            HistoricoLeiturasDialog.setObjectName(u"HistoricoLeiturasDialog")
        HistoricoLeiturasDialog.resize(960, 680)
        HistoricoLeiturasDialog.setModal(True)
        self.layout_principal = QVBoxLayout(HistoricoLeiturasDialog)
        self.layout_principal.setObjectName(u"layout_principal")
        self.lbl_titulo = QLabel(HistoricoLeiturasDialog)
        self.lbl_titulo.setObjectName(u"lbl_titulo")
        self.lbl_titulo.setStyleSheet(u"font-weight: bold; font-size: 14px;")

        self.layout_principal.addWidget(self.lbl_titulo)

        self.layout_filtro_periodo = QHBoxLayout()
        self.layout_filtro_periodo.setObjectName(u"layout_filtro_periodo")
        self.lbl_periodo = QLabel(HistoricoLeiturasDialog)
        self.lbl_periodo.setObjectName(u"lbl_periodo")

        self.layout_filtro_periodo.addWidget(self.lbl_periodo)

        self.combo_periodo = QComboBox(HistoricoLeiturasDialog)
        self.combo_periodo.addItem("")
        self.combo_periodo.addItem("")
        self.combo_periodo.addItem("")
        self.combo_periodo.addItem("")
        self.combo_periodo.addItem("")
        self.combo_periodo.setObjectName(u"combo_periodo")

        self.layout_filtro_periodo.addWidget(self.combo_periodo)

        self.lbl_inicio = QLabel(HistoricoLeiturasDialog)
        self.lbl_inicio.setObjectName(u"lbl_inicio")

        self.layout_filtro_periodo.addWidget(self.lbl_inicio)

        self.dt_inicio = QDateTimeEdit(HistoricoLeiturasDialog)
        self.dt_inicio.setObjectName(u"dt_inicio")
        self.dt_inicio.setCalendarPopup(True)

        self.layout_filtro_periodo.addWidget(self.dt_inicio)

        self.lbl_fim = QLabel(HistoricoLeiturasDialog)
        self.lbl_fim.setObjectName(u"lbl_fim")

        self.layout_filtro_periodo.addWidget(self.lbl_fim)

        self.dt_fim = QDateTimeEdit(HistoricoLeiturasDialog)
        self.dt_fim.setObjectName(u"dt_fim")
        self.dt_fim.setCalendarPopup(True)

        self.layout_filtro_periodo.addWidget(self.dt_fim)

        self.btn_consultar = QPushButton(HistoricoLeiturasDialog)
        self.btn_consultar.setObjectName(u"btn_consultar")

        self.layout_filtro_periodo.addWidget(self.btn_consultar)

        self.spacer_filtro_periodo = QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.layout_filtro_periodo.addItem(self.spacer_filtro_periodo)


        self.layout_principal.addLayout(self.layout_filtro_periodo)

        self.lbl_estado = QLabel(HistoricoLeiturasDialog)
        self.lbl_estado.setObjectName(u"lbl_estado")
        self.lbl_estado.setWordWrap(True)

        self.layout_principal.addWidget(self.lbl_estado)

        self.chart_historico = QWidget(HistoricoLeiturasDialog)
        self.chart_historico.setObjectName(u"chart_historico")
        self.chart_historico.setMinimumSize(QSize(0, 300))

        self.layout_principal.addWidget(self.chart_historico)

        self.table_resumo = QTableWidget(HistoricoLeiturasDialog)
        if (self.table_resumo.columnCount() < 4):
            self.table_resumo.setColumnCount(4)
        __qtablewidgetitem = QTableWidgetItem()
        self.table_resumo.setHorizontalHeaderItem(0, __qtablewidgetitem)
        __qtablewidgetitem1 = QTableWidgetItem()
        self.table_resumo.setHorizontalHeaderItem(1, __qtablewidgetitem1)
        __qtablewidgetitem2 = QTableWidgetItem()
        self.table_resumo.setHorizontalHeaderItem(2, __qtablewidgetitem2)
        __qtablewidgetitem3 = QTableWidgetItem()
        self.table_resumo.setHorizontalHeaderItem(3, __qtablewidgetitem3)
        self.table_resumo.setObjectName(u"table_resumo")
        self.table_resumo.setMaximumSize(QSize(16777215, 150))
        self.table_resumo.setAlternatingRowColors(True)
        self.table_resumo.setColumnCount(4)
        self.table_resumo.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_resumo.setSelectionBehavior(QAbstractItemView.SelectRows)

        self.layout_principal.addWidget(self.table_resumo)

        self.buttonBox = QDialogButtonBox(HistoricoLeiturasDialog)
        self.buttonBox.setObjectName(u"buttonBox")
        self.buttonBox.setOrientation(Qt.Horizontal)
        self.buttonBox.setStandardButtons(QDialogButtonBox.Close)

        self.layout_principal.addWidget(self.buttonBox)


        self.retranslateUi(HistoricoLeiturasDialog)

        QMetaObject.connectSlotsByName(HistoricoLeiturasDialog)
    # setupUi

    def retranslateUi(self, HistoricoLeiturasDialog):
        HistoricoLeiturasDialog.setWindowTitle(QCoreApplication.translate("HistoricoLeiturasDialog", u"Hist\u00f3rico de Leituras", None))
        self.lbl_titulo.setText(QCoreApplication.translate("HistoricoLeiturasDialog", u"Hist\u00f3rico de Leituras", None))
        self.lbl_periodo.setText(QCoreApplication.translate("HistoricoLeiturasDialog", u"Per\u00edodo:", None))
        self.combo_periodo.setItemText(0, QCoreApplication.translate("HistoricoLeiturasDialog", u"\u00daltima hora", None))
        self.combo_periodo.setItemText(1, QCoreApplication.translate("HistoricoLeiturasDialog", u"\u00daltimas 6 horas", None))
        self.combo_periodo.setItemText(2, QCoreApplication.translate("HistoricoLeiturasDialog", u"\u00daltimas 24 horas", None))
        self.combo_periodo.setItemText(3, QCoreApplication.translate("HistoricoLeiturasDialog", u"\u00daltimos 7 dias", None))
        self.combo_periodo.setItemText(4, QCoreApplication.translate("HistoricoLeiturasDialog", u"Personalizado", None))
        self.lbl_inicio.setText(QCoreApplication.translate("HistoricoLeiturasDialog", u"De:", None))
        self.dt_inicio.setDisplayFormat(QCoreApplication.translate("HistoricoLeiturasDialog", u"dd/MM/yyyy HH:mm:ss", None))
        self.lbl_fim.setText(QCoreApplication.translate("HistoricoLeiturasDialog", u"At\u00e9:", None))
        self.dt_fim.setDisplayFormat(QCoreApplication.translate("HistoricoLeiturasDialog", u"dd/MM/yyyy HH:mm:ss", None))
        self.btn_consultar.setText(QCoreApplication.translate("HistoricoLeiturasDialog", u"Consultar", None))
        self.lbl_estado.setText(QCoreApplication.translate("HistoricoLeiturasDialog", u"Escolha o per\u00edodo e clique em Consultar.", None))
        ___qtablewidgetitem = self.table_resumo.horizontalHeaderItem(0)
        ___qtablewidgetitem.setText(QCoreApplication.translate("HistoricoLeiturasDialog", u"Sensor", None))
        ___qtablewidgetitem1 = self.table_resumo.horizontalHeaderItem(1)
        ___qtablewidgetitem1.setText(QCoreApplication.translate("HistoricoLeiturasDialog", u"M\u00ednimo", None))
        ___qtablewidgetitem2 = self.table_resumo.horizontalHeaderItem(2)
        ___qtablewidgetitem2.setText(QCoreApplication.translate("HistoricoLeiturasDialog", u"M\u00e9dia", None))
        ___qtablewidgetitem3 = self.table_resumo.horizontalHeaderItem(3)
        ___qtablewidgetitem3.setText(QCoreApplication.translate("HistoricoLeiturasDialog", u"M\u00e1ximo", None))
    # retranslateUi
