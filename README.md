# Controle de Reservatório de Água

Sistema de Supervisão e Controle Desktop (SCADA) para monitoramento de qualidade e nível de reservatórios de água, desenvolvido para a disciplina **Desenvolvimento de Aplicações Computacionais**.

> 🚧 Em desenvolvimento

## Sobre o projeto

A aplicação atua como uma Interface Homem-Máquina (IHM) de supervisão, recebendo leituras de sensores de um microcontrolador **STM32F103C8T6** ("Blue Pill") e permitindo a intervenção do operador em tempo real sobre o reservatório monitorado.

### Hardware já disponível

| Componente | Tipo | Peça |
|---|---|---|
| Microcontrolador | — | STM32F103C8T6 ("Blue Pill") |
| Temperatura da água | Sensor digital | DS18B20 (sonda à prova d'água) |
| TDS / condutividade | Sensor analógico | Sensor analógico de condutividade de água |
| Turbidez | Sensor analógico | Sensor de turbidez para monitoramento de água |

Sem a placa conectada o programa roda com dados **simulados**; ao conectar o STM32 (USB) ele passa sozinho para a **leitura real** (ver *Protocolo serial e handshake* abaixo).

## Stack

- **Python 3.14**
- **[PySide6](https://doc.qt.io/qtforpython/)** (Qt for Python) para a interface gráfica
- **Qt Designer** para as telas (`.ui`), compiladas via `pyside6-uic`

## Como rodar

```bash
git clone https://github.com/DellaliberaPedro/ControleReservatoriodeAgua.git
cd ControleReservatoriodeAgua

python -m venv .venv
.venv\Scripts\activate        # Windows (PowerShell)
# source .venv/bin/activate     # Linux/Mac

pip install -r requirements.txt
python main.py
```

## Arquitetura (MVC)

```
ControleReservatoriodeAgua/
├── main.py                      # ponto de entrada enxuto (log -> config -> banco -> janela)
├── requirements.txt
├── .gitignore
├── controllers/                 # lógica das janelas, sinais/slots e orquestração
│   ├── maincontroller.py        # janela principal (dashboard)
│   ├── settingscontroller.py    # modal de regras de alerta (com validação)
│   ├── historicocontroller.py   # janela de histórico de eventos
│   └── historicoleiturascontroller.py  # janela de histórico de leituras (gráfico + resumo por período)
├── database/                    # conexão SQLite (timeout, PRAGMA foreign_keys, init_db)
├── models/                      # entidades, repositórios (SQL com '?'), config e log
│   ├── sensor_data.py           # estruturas de dados (leitura, evento, regra, dispositivo)
│   └── data_simulator.py        # gerador de leituras simuladas
├── ui/                          # telas do Qt Designer (.ui -> Ui_*.py), sem regra de negócio
├── data/                        # config.json e smartgrid.db (gerados na 1ª execução)
├── logs/                        # smartgrid.log (auditoria)
└── resources/
    └── style.qss                # tema visual (escuro)
```

Regra de organização: nada de lógica de negócio dentro de `ui/` — os arquivos `Ui_*.py` são gerados automaticamente (`pyside6-uic`) e não devem ser editados à mão. Depois de mexer num `.ui` no Qt Designer, regenera com:

```bash
pyside6-uic ui/tela.ui -o ui/Ui_tela.py
pyside6-uic ui/tela_configuracoes.ui -o ui/Ui_tela_configuracoes.py
pyside6-uic ui/tela_historico_leituras.ui -o ui/Ui_tela_historico_leituras.py
```

## Persistência, logs e demonstração de falhas (A1/2)

Na primeira execução são criados automaticamente:

| Arquivo | Conteúdo |
|---|---|
| `data/smartgrid.db` | SQLite: `dispositivos` (VALV-01, RELE-EMG), `eventos` (histórico), `regras_alerta` e `leituras` (série temporal dos sensores) |
| `data/config.json` | porta COM, baud rate, timeout, limites iniciais e intervalo de atualização |
| `logs/smartgrid.log` | auditoria com data/hora e severidade `[INFO]`, `[WARNING]`, `[ERROR]` |

Roteiro da arguição:

1. **Falha de validação:** *Configurar Regras de Alerta...* → dê duplo clique no valor de uma regra e digite `abc`, `-5` ou deixe em branco → *OK*. Aparece um `QMessageBox` e nada é salvo.
2. **Rollback:** menu *Diagnóstico → Simular falha no próximo corte emergencial* (`Ctrl+Shift+F`) → *CORTE EMERGENCIAL* → *Yes*. A operação falha entre o UPDATE dos dispositivos e o INSERT do evento; a válvula continua **ABERTA** e nenhum evento novo aparece no histórico.
3. **Auditoria:** *Diagnóstico → Abrir arquivo de log* (`Ctrl+L`) e mostre as linhas `[WARNING] Entrada inválida bloqueada...` e `[ERROR] Falha na operação de corte emergencial. Rollback executado: ...`.

## Concorrência: três threads (Critério 1)

O programa executa em **três threads** com papéis separados. Nenhuma toca no objeto da outra: conversam só por **fila** (`queue.Queue`) e por **sinais do Qt**.

| Thread | Classe | O que faz |
|---|---|---|
| **GUI** | `MainController` (janela) | desenha a tela e trata cliques; nunca abre o SQLite depois que a janela está no ar |
| **Serial** | `SerialWorker` (`models/serial_worker.py`) | abre a porta, faz o handshake, lê o STM32 e escreve os comandos |
| **Banco** | `DatabaseWorker` (`models/database_worker.py`) | grava e consulta o SQLite |

```
GUI ──── queue.Queue (FIFO) ────▶ DatabaseWorker.executar()   [thread do banco]
GUI ◀─── sinal concluido(token, resultado) ──── DatabaseWorker
GUI ◀─── sinais (leitura_recebida, conectado, ...) ──── SerialWorker  [thread serial]
```

- A GUI chama `banco.executar("inserir_evento", evento, ao_terminar=funcao)` e a chamada **volta na hora**: só põe o pedido na fila. A tela nunca espera o disco.
- A thread do banco atende a fila na ordem em que chegou (FIFO), uma transação por operação, e devolve um `ResultadoBanco` (erro de banco vira `ok=False`, nunca derruba a thread).
- Só operações de uma lista fixa (`OPERACOES`) são aceitas; os argumentos são **copiados** antes de entrar na fila (nenhum objeto mutável é dividido entre threads).
- Fechar a janela coloca um marcador de fim na fila: tudo que já foi pedido é gravado antes de a thread terminar.
- Ao iniciar, o `logs/smartgrid.log` mostra os ids das três threads (`GUI thread`, `Thread de banco`, `Thread serial`).
- **Com a placa conectada a tela segue o hardware na hora** (a placa confirmou, o operador vê) e o banco grava em segundo plano. No modo simulado grava primeiro e só depois muda a tela (é isso que permite a demonstração do rollback).
- Exceção honesta: a carga inicial (regras, estado da válvula, criação das tabelas) e a escrita de `config.json`/log ainda acontecem na GUI thread, antes da janela aparecer.

## Protocolo serial e handshake (Critério 2)

Tudo é texto ASCII terminado em `\n`. A placa **liga calada, a 9600 baud**, e só manda leituras depois do handshake:

```
PC                                    STM32 (firmware v5)
 │  abre a porta a 9600 bps             │  AGUARDANDO (calada, bomba desligada)
 │── HELLO ───────────────────────────▶│
 │◀── H,READY,5.0,9600;19200;38400;115200
 │── BAUD,115200 ─────────────────────▶│  (n tem de estar na lista da placa)
 │◀── H,BAUD,115200 ───────────────────│  (enviado ainda no baud antigo; depois os dois trocam)
 │  conexao.baudrate = 115200           │
 │── START ───────────────────────────▶│
 │◀── H,START ─────────────────────────│  TELEMETRIA
 │◀── L,<tds>,<turbidez>,<temperatura> ─│  a cada 2 s
```

- **Negociação:** o PC escolhe o baud que os dois lados sabem falar e que mais se aproxima do desejado na tela (`escolher_baud`). Se a placa recusar (`H,ERRO,BAUD`), a conversa **continua a 9600** (fallback). Bauds suportados: **9600, 19200, 38400, 115200**.
- **Falhas:** cada passo reenvia o comando se a resposta não chega (4 tentativas no `HELLO`, 3 nos demais) e desiste com mensagem clara; firmware antigo (que transmite sem handshake) tem aviso próprio. Linhas corrompidas nunca derrubam o programa.
- **Segurança:** sem handshake a placa recusa `RELE_ON` e `LIMITE` (`H,ERRO,ESTADO`); `RELE_OFF` vale sempre. Um novo `HELLO` volta a placa a AGUARDANDO com a bomba **desligada**.
- Comandos depois do handshake: `RELE_ON`, `RELE_OFF`, `LIMITE,<TDS|TURBIDEZ|TEMPERATURA>,<valor>`; confirmações `A,...`; eventos espontâneos `E,LIMITE,<sensor>`, `E,BOTAO`, `E,BLOQUEADO`.
- A lógica de decisão é uma **máquina de estados pura** (`models/handshake.py`, sem Qt e sem porta serial): o `SerialWorker` só faz a E/S.

> **Honestidade técnica:** a Blue Pill usa a porta **USB CDC**, em que o baud é *virtual* (a velocidade real é a do USB). A negociação acontece de verdade no **protocolo** (lista, escolha, confirmação, fallback) e o PC reconfigura `baudrate` da porta, mas o número não muda o ritmo elétrico. O firmware tem a chave `SERIAL_E_USB_CDC`: com `0` (UART real, ex.: adaptador USB-TTL em PA9/PA10) a placa chama `Serial.begin(n)` de verdade.

O firmware fica em `firmware/firmware_reservatorio/` (Arduino IDE + core STM32duino, gravado por ST-Link).

## Persistência híbrida e histórico de leituras (Critério 3)

Cada tipo de dado vai para o armazenamento que combina com ele:

| Dado | Onde fica | Por quê |
|---|---|---|
| Leituras dos sensores (série temporal) | SQLite, tabela `leituras` | volume alto, consulta por período |
| Eventos, regras de alerta, estado dos dispositivos | SQLite (`eventos`, `regras_alerta`, `dispositivos`) | dados relacionais, com chave estrangeira e transação |
| Porta COM, baud desejado, timeout, limites iniciais | `data/config.json` | poucos parâmetros que o operador pode editar à mão |
| Auditoria (quem fez o quê e quando) | `logs/smartgrid.log` | texto sequencial, sobrevive mesmo se o banco falhar |

**Gráfico em tempo real.** O gráfico da tela principal é alimentado direto pela leitura que acabou de chegar (listas em memória), sem depender do banco. Se o banco travar, o gráfico continua andando.

**Gravação das leituras.** Toda leitura vinda da **placa** vira uma linha em `leituras` (data/hora, temperatura, TDS, turbidez e IQA). O pedido vai para a fila da thread do banco e a tela **não espera**. Leituras do simulador **não** são gravadas (não sujam a série). Se o banco não conseguir gravar, o operador é avisado **uma vez** (barra de status e evento de alerta); o resto vai para o `smartgrid.log`, e o aviso volta a valer se o banco se recuperar e falhar de novo.

**Histórico assíncrono.** *Históricos → Abrir Histórico de Leituras...* abre a janela com os períodos *Última hora*, *6 horas*, *24 horas*, *7 dias* ou *Personalizado* (campos De/Até). Ao clicar em *Consultar* a janela pede à **thread do banco** e continua respondendo; o botão fica desabilitado até a resposta voltar, período inválido (início depois do fim) nem chega ao banco e uma resposta que chegue depois de a janela fechar é descartada.

**Muitos dados sem travar o gráfico.** Uma semana de leituras a cada 2 s tem cerca de 300 mil linhas, pesado demais para desenhar. A consulta **agrupa no próprio SQLite**: o período é dividido, a partir do início, em intervalos iguais e cada intervalo vira um ponto com a média das leituras dele, no máximo `MAX_PONTOS_GRAFICO` (1200). O índice `idx_leituras_data_hora` evita varrer a tabela inteira. O **mínimo, a média e o máximo** da tabela de resumo usam *todas* as leituras (a média é ponderada pelo número de leituras de cada intervalo), então não dependem do agrupamento.

> **Medido (ambiente de desenvolvimento, SQLite local):** 300 mil leituras (uma semana) ocupam cerca de 24 MB e a consulta de 7 dias volta em menos de 0,5 s; uma hora de leituras volta em ~10 ms. Em outro computador os tempos mudam. Ainda **não há política de limpeza** da tabela: ela só cresce (cerca de 43 mil linhas por dia de operação contínua).

## Testes automatizados

Os testes da camada pura (protocolo e máquina de estados do handshake, e a série temporal de leituras em SQLite temporário; sem Qt e sem porta serial) ficam em `tests/`:

```bash
pip install pytest
python -m pytest tests -q
```

## Sobre o .gitignore

O `.gitignore` diz pro Git quais arquivos **não** devem entrar no repositório: a pasta `.venv/` (ambiente virtual — cada um cria o seu localmente), `__pycache__/` e `*.pyc` (bytecode compilado, gerado automaticamente toda vez que o programa roda) e configurações de IDE. Sem isso, esses arquivos mudam sozinhos a cada execução e geram conflito toda hora entre quem está usando o projeto. Se você clonar o projeto e não tiver esse arquivo, copia o conteúdo abaixo pra um arquivo chamado `.gitignore` na raiz:

```
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/
env/
.vscode/
.idea/
.DS_Store
Thumbs.db
*.log
*.db
*.sqlite3
```

## Fluxo de trabalho em equipe

- Antes de começar a mexer, sempre dar `git pull origin main` primeiro, pra pegar o que os outros já fizeram.
- Cada um commita com a própria conta GitHub — a atividade audita isso individualmente.
- Prefira commits pequenos e frequentes (com mensagem clara do que mudou) a um commit gigante no final.

## Equipe

- Pedro Henrique Devens Dellalibera
- João Gabriel Trevisol
- Alan Hoffmann dos Santos
- Diogo Hoffmann do Prado

## 💻 Linguagens e Tecnologias

<p align="center">

![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![PySide6](https://img.shields.io/badge/PySide6-6.11.1-41CD52?logo=qt&logoColor=white)
![STM32](https://img.shields.io/badge/STM32F103C8T6-Blue%20Pill-03234B?logo=stmicroelectronics&logoColor=white)

</p>

### 🧑‍💻 Linguagens de programação

| Linguagem     |     Versão | Utilização                           |
| ------------- | ---------: | ------------------------------------ |
| 🐍 **Python** |   **3.14** | Desenvolvimento da aplicação desktop |
| ⚙️ **C++ (Arduino)** | — | Firmware do STM32 (`firmware/`)      |
| 🎨 **QSS**    | **Qt 6.x** | Estilização da interface gráfica     |

### 🖥️ Frameworks e bibliotecas

| Tecnologia      |  Versão | Utilização                           |
| --------------- | ------: | ------------------------------------ |
| **PySide6**     | **6.11.1** | Desenvolvimento da interface gráfica |
| **Qt**          | **6.11.1** | Framework da aplicação desktop       |
| **Qt Designer** | **6.11.1** | Criação das interfaces `.ui`         |

### 🔌 Hardware e firmware

| Tecnologia             | Versão / Modelo | Utilização                               |
| ---------------------- | --------------: | ---------------------------------------- |
| **STM32F103C8T6**      |       Blue Pill | Microcontrolador principal               |
| **Arduino IDE + core STM32duino** |   — | Compilação do firmware (gravação por ST-Link) |
| **DS18B20**            |               — | Sensor de temperatura                    |
| **Sensor TDS**         |               — | Medição de condutividade/TDS             |
| **Sensor de Turbidez** |               — | Medição da turbidez da água              |

### 🛠️ Ferramentas de desenvolvimento

| Ferramenta             |  Versão | Utilização                     |
| ---------------------- | ------: | ------------------------------ |
| **Git**                | **2.55.0** | Controle de versão             |
| **GitHub**             |       — | Hospedagem do código-fonte     |
| **Visual Studio Code** | **1.142.0** | Editor de código               |
| **Qt Designer**        | **6.11.1** | Desenvolvimento das interfaces |

> 📌 **Observação:** as versões indicadas correspondem às versões utilizadas ou previstas durante o desenvolvimento do projeto e podem ser atualizadas conforme a evolução da aplicação.
