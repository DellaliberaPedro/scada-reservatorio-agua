/*
  Firmware de leitura de sensores — Projeto Reservatório
  Placa: STM32F103C8T6 "Blue Pill" (core STM32duino)

  Responsabilidade deste firmware: SO ler sensores e acionar o rele/bomba.
  Toda a logica de negocio (calculo de IQA, setpoints, persistencia, log)
  continua no software Python ja pronto — aqui a placa so "fala" o protocolo.

  Protocolo serial (v5): a placa liga CALADA a 9600 baud, esperando o handshake do SCADA.
    Handshake (a placa responde ao HELLO em QUALQUER estado):
      PC -> placa:  HELLO                 placa -> PC:  H,READY,<firmware>,<bauds>   ex.: H,READY,5.0,9600;19200;38400;115200
      PC -> placa:  BAUD,<n>              placa -> PC:  H,BAUD,<n>   (enviado ainda no baud antigo; depois os dois trocam)
                                                        H,ERRO,BAUD  (n fora da lista: segue tudo a 9600)
      PC -> placa:  START                 placa -> PC:  H,START      (so agora a placa comeca a mandar as leituras)
      Fora de hora (RELE_ON/LIMITE sem handshake, BAUD depois do START): H,ERRO,ESTADO. RELE_OFF vale sempre.
    Leitura, enviada a cada LEITURA_INTERVALO_MS (so depois do START):
      L,<tds>,<turbidez>,<temperatura>\n
      ex: L,245.30,3.20,24.10\n
    Comando (recebido do PC):
      RELE_ON\n
      RELE_OFF\n
      LIMITE,<TDS|TURBIDEZ|TEMPERATURA>,<valor>\n   muda o limite de corte daquele sensor
      ex: LIMITE,TDS,200\n   (vale ate a placa reiniciar; ao ligar volta ao padrao)
    Confirmacao (enviada de volta):
      A,<comando>\n
      ex: A,RELE_ON\n
      A,LIMITE,<sensor>,<valor>\n   (ex: A,LIMITE,TDS,200.00)
    Evento (a placa avisa sozinha, sem o PC pedir):
      E,LIMITE,<TDS|TURBIDEZ|TEMPERATURA>\n   a placa cortou a bomba por limite
      E,BLOQUEADO\n                           RELE_ON recusado (acima do limite ou botao pressionado)
      E,BOTAO\n                               botao de emergencia apertado: bomba cortada

  HISTORICO DE AJUSTES (feitos na bancada real, sessao de 01/11/2026):
    - PINO_ONEWIRE foi movido de PB0 para PB10, porque o fio de dados do
      DS18B20 ja estava fisicamente ligado em PB10 na montagem real (em vez
      de rewirear, o Pedro pediu para ajustar o firmware).
    - Os 3 sensores foram confirmados funcionando via Serial Monitor:
        temperatura: 22-29 C (faixa normal de ambiente)
        TDS: ~54 ppm em agua de torneira normal (correto, "OK")
        turbidez (valor bruto, ainda sem curva calibrada): ~146-147 com a
          sonda aberta/exposta (baseline) e ~198 com a sonda totalmente
          coberta/bloqueada (pior caso observado)
    - TURBIDEZ_TENSAO_CLARA / TURBIDEZ_TENSAO_TURVA abaixo SAO AINDA
      PLACEHOLDERS (3.00V / 1.50V) — a calibracao real da turbidez em
      unidades fisicas (NTU ou equivalente) ainda nao foi feita porque
      faltava uma amostra de agua turva de verdade. Plano: usar o painel
      Python (painel_reservatorio.py, neste mesmo pacote) para gravar um
      CSV com leituras reais de agua turva, e recalcular essas constantes
      (ou substituir a formula linear por uma curva real) a partir desse
      CSV.

  HISTORICO DE AJUSTES (19/11/2026, rele + bomba reais):
    - O modulo de rele comprado e ATIVO-BAIXO e tem um resistor que segura o
      IN em 5 V. Saida normal (push-pull) nao serve: 3,3 V no IN ainda deixa
      o rele ligado, entao ele nunca desligava. Por isso o PB12 agora trabalha
      em open-drain: LOW liga o rele; HIGH SOLTA o pino e o resistor do modulo
      leva o IN a 5 V (rele desligado). A pinagem nao mudou.
    - Corte automatico: se TDS, turbidez ou temperatura passar do limite
      (constantes LIMITE_* abaixo), o proprio firmware desliga a bomba na
      hora, sem esperar o PC, e recusa RELE_ON enquanto a leitura continuar
      acima. A bomba NUNCA religa sozinha: so volta com um novo RELE_ON.
    - O DS18B20 falha (-127) enquanto a bomba roda (ruido do motor). Sem
      temperatura valida o TDS zerava e o limite de TDS ficava cego. Agora o
      firmware segura o ultimo valor bom da temperatura por ate
      FALHAS_TEMPERATURA_TOLERADAS leituras seguidas; passando disso volta a
      mandar -127 (sensor sumiu de verdade). Se nunca houve leitura boa, o TDS
      e calculado a 25 C em vez de zerar.
    - Botao de emergencia no PB9 (botao tatil, um lado no PB9, o outro no GND,
      pull-up interna). Apertou -> a bomba e cortada em ate ~40 ms, a placa manda
      E,BOTAO e recusa RELE_ON enquanto o botao estiver apertado. Soltou -> a bomba
      continua desligada; so volta com um novo RELE_ON. O botao e lido o tempo todo,
      inclusive durante as esperas dos sensores (conversao do DS18B20 e ADC).
      E um corte por software (passa pelo STM32), nao um corte eletrico direto.
    - Limites ajustaveis pelo SCADA: os tres limites deixaram de ser constantes
      fixas e viraram variaveis. Ao ligar valem os PADRAO (250 ppm / 185 / 35 C).
      O SCADA manda os limites da tela logo ao conectar e a cada mudanca, com o
      comando LIMITE,<sensor>,<valor>; a placa confirma com A,LIMITE,<sensor>,<valor>.
      O corte continua sendo feito AQUI, na placa, sem esperar o PC. Valor invalido
      (texto, zero, negativo, absurdo) ou sensor desconhecido: ignorado, sem confirmacao.

  HISTORICO DE AJUSTES (22/11/2026, v5 - handshake e negociacao de baud, Criterio 2 do trabalho final):
    - A placa agora liga em AGUARDANDO: calada, a 9600 baud, bomba desligada. So depois de
      HELLO -> H,READY -> BAUD,<n> -> H,BAUD -> START -> H,START ela entra em TELEMETRIA e manda as
      linhas "L,". HELLO funciona em qualquer estado e volta a placa para AGUARDANDO (bomba desligada:
      sem supervisao valida, estado seguro). START repetido e inofensivo (responde H,START de novo).
    - BAUD,<n>: n tem de estar em BAUDS_SUPORTADOS (9600, 19200, 38400, 115200). Se o PC mandar BAUD e
      nao completar o START em HANDSHAKE_TIMEOUT_MS, a placa volta sozinha para 9600.
    - IMPORTANTE (honestidade tecnica): na porta USB CDC da Blue Pill (SERIAL_E_USB_CDC 1) o baud e
      VIRTUAL: a velocidade real e a do USB, e o valor da linha so e guardado, nao reconfigura nada.
      A negociacao acontece de verdade no PROTOCOLO (lista de bauds, escolha, confirmacao, fallback),
      e com SERIAL_E_USB_CDC 0 (UART real, ex.: USB-TTL em PA9/PA10) a placa chama Serial.begin(n) de verdade.
    - Linhas de comando maiores que TAMANHO_MAX_COMANDO sao descartadas (ruido de baud errado nao enche a RAM).
*/

#include <OneWire.h>
#include <DallasTemperature.h>

// ---------- Pinagem ----------
#define PINO_TDS       PA0   // saída analógica do sensor de TDS/condutividade
#define PINO_TURBIDEZ  PA1   // saída analógica do sensor de turbidez (ST-100)
#define PINO_ONEWIRE   PB10  // barramento 1-Wire do DS18B20 (ajustado de PB0 -> PB10 na bancada real)
#define PINO_RELE      PB12  // IN do módulo relé (ativo-baixo, usado em open-drain)
#define PINO_BOTAO     PB9   // botão de emergência (normalmente aberto): um lado no PB9, o outro no GND

// ---------- Configuração geral ----------
// 1 = Blue Pill pelo cabo USB (CDC): o baud e virtual, Serial.begin() nao e chamado de novo (evita re-enumerar o USB).
// 0 = UART real (ex.: adaptador USB-TTL em PA9/PA10): a troca de baud chama Serial.begin(n) de verdade.
#ifndef SERIAL_E_USB_CDC
#define SERIAL_E_USB_CDC 1
#endif

const char*    FW_VERSAO             = "5.0";
const uint32_t BAUD_INICIAL          = 9600;   // a placa liga aqui e espera o HELLO
const uint32_t BAUDS_SUPORTADOS[]    = {9600, 19200, 38400, 115200};
const uint8_t  NUM_BAUDS             = sizeof(BAUDS_SUPORTADOS) / sizeof(BAUDS_SUPORTADOS[0]);
const unsigned long HANDSHAKE_TIMEOUT_MS = 10000; // BAUD sem START depois disso: volta a 9600
const unsigned int  TAMANHO_MAX_COMANDO  = 64;    // linha maior que isso e lixo (ex.: baud errado): descartada
const uint32_t LEITURA_INTERVALO_MS  = 2000;   // intervalo entre leituras enviadas
const uint8_t  ADC_RESOLUCAO_BITS    = 12;     // ADC do STM32F103 é de 12 bits
const float    ADC_VALOR_MAXIMO      = 4095.0; // 2^12 - 1
const float    TENSAO_REFERENCIA     = 3.3;    // referência do ADC (3.3V)

// ---------- TODO: calibrar a turbidez (pendente, ver histórico acima) ----------
// Sem curva de calibração real ainda. Meça a tensão com água limpa (referência
// "clara") e com uma amostra turva conhecida, e ajuste as duas constantes
// abaixo. Até calibrar, o número enviado é só uma estimativa provisória.
const float TURBIDEZ_TENSAO_CLARA = 3.00; // TODO: medir com água limpa
const float TURBIDEZ_TENSAO_TURVA = 1.50; // TODO: medir com amostra turva de referência

// ---------- Limites de proteção (corte automático da bomba) ----------
// Passou de QUALQUER limite -> o firmware desliga o relé/bomba na hora e só
// aceita RELE_ON de novo quando a leitura voltar para dentro do limite.
// Ajuste os valores aqui. A turbidez está na unidade provisória do firmware
// (baseline ~146 com a sonda exposta, ~198 com a sonda coberta).
// Os valores abaixo são os PADRÃO (valem ao ligar a placa). O SCADA troca os limites
// em tempo de execução (comando LIMITE,...): ficam só na RAM, reiniciou = volta ao padrão.
const float LIMITE_TDS_PPM_PADRAO       = 250.0; // faixa normal com a bomba ligada: 100-180 ppm (inclui ruído do motor)
const float LIMITE_TURBIDEZ_PADRAO      = 185.0; // provisório, até calibrar a turbidez
const float LIMITE_TEMPERATURA_C_PADRAO = 35.0;  // temperatura ambiente medida: 22-29 C
const float LIMITE_MAXIMO_ACEITO        = 10000.0; // recusa valores absurdos vindos da serial

float limiteTdsPpm       = LIMITE_TDS_PPM_PADRAO;
float limiteTurbidez     = LIMITE_TURBIDEZ_PADRAO;
float limiteTemperaturaC = LIMITE_TEMPERATURA_C_PADRAO;

uint32_t baudAtual = BAUD_INICIAL;   // baud em que a conversa está agora
bool telemetriaAtiva = false;        // false = AGUARDANDO (calada, esperando o handshake); true = TELEMETRIA
unsigned long baudTrocadoMs = 0;     // quando o último BAUD,<n> foi aceito (para o timeout do handshake)

bool bombaLigada   = false; // estado do relé (false = desligado)
bool acimaDoLimite = false; // true enquanto a última leitura passar de algum limite

// ---------- Botão de emergência ----------
const unsigned long BOTAO_DEBOUNCE_MS = 30;    // o botão precisa ficar apertado 30 ms seguidos (ignora trepidação)
bool botaoPressionado = false;                 // true enquanto o botão estiver apertado

// ---------- Tolerância a falhas do DS18B20 (ruído do motor da bomba) ----------
// Quando a leitura falha (-127), o firmware reaproveita o último valor bom. A água
// muda de temperatura devagar, então segurar o valor por ~30 s é seguro.
const uint8_t FALHAS_TEMPERATURA_TOLERADAS = 15; // leituras seguidas (~2 s cada)
float   ultimaTemperaturaValida = 25.0;
bool    temperaturaJaLida       = false;
uint8_t falhasTemperatura       = 0;

OneWire barramentoOneWire(PINO_ONEWIRE);
DallasTemperature sensorTemperatura(&barramentoOneWire);

unsigned long ultimaLeituraMs = 0;
String comandoRecebido = "";

// ---------- Relé da bomba ----------
// O módulo (1 canal, SRD-05VDC-SL-C) é de NÍVEL BAIXO e tem um resistor que segura
// o IN em 5 V. Por isso o pino trabalha em open-drain:
//   LOW  = puxa o IN para o GND                  -> relé LIGA    (bomba liga)
//   HIGH = solta o pino; o resistor do módulo leva o IN a 5 V -> relé DESLIGA
// Saída normal (push-pull) não serve: 3,3 V no IN ainda deixa o relé ligado.
void releIniciar() {
  pinMode(PINO_RELE, OUTPUT_OPEN_DRAIN);
  digitalWrite(PINO_RELE, HIGH); // solto = relé desligado = bomba desligada
  bombaLigada = false;
}

void releLigar() {
  digitalWrite(PINO_RELE, LOW);
  bombaLigada = true;
}

void releDesligar() {
  digitalWrite(PINO_RELE, HIGH);
  bombaLigada = false;
}

// ---------- Botão de emergência ----------
// Lido por polling (sem interrupção). Apertou e segurou 30 ms -> corta a bomba e avisa
// o PC UMA vez por aperto. Enquanto estiver apertado, RELE_ON é recusado.
void verificarBotao() {
  static bool contando = false;
  static unsigned long inicioMs = 0;

  if (digitalRead(PINO_BOTAO) == LOW) {            // apertado (pull-up interna: aperta = LOW)
    if (!contando) {
      contando = true;
      inicioMs = millis();
    }
    if (!botaoPressionado && millis() - inicioMs >= BOTAO_DEBOUNCE_MS) {
      botaoPressionado = true;
      releDesligar();
      if (telemetriaAtiva) Serial.print("E,BOTAO\n"); // antes do handshake a bomba nem pode estar ligada: sem aviso
    }
  } else {                                         // solto
    contando = false;
    botaoPressionado = false;
  }
}

// Substitui o delay(): espera, mas continua olhando o botão o tempo todo.
void esperar(unsigned long ms) {
  unsigned long inicio = millis();
  while (millis() - inicio < ms) {
    verificarBotao();
  }
}

void setup() {
  releIniciar(); // primeira coisa: o pino fica solto (relé desligado) pelo menor tempo possível
  pinMode(PINO_BOTAO, INPUT_PULLUP);
  Serial.begin(BAUD_INICIAL);
  baudAtual = BAUD_INICIAL;
  analogReadResolution(ADC_RESOLUCAO_BITS);

  sensorTemperatura.begin();
  sensorTemperatura.setWaitForConversion(false); // não trava esperando o DS18B20: a espera é feita em esperar()

  delay(500); // dá tempo pro Monitor Serial conectar antes da primeira leitura
}

void loop() {
  verificarBotao();
  processarComandosSerial();
  verificarTempoDoHandshake();

  if (telemetriaAtiva && millis() - ultimaLeituraMs >= LEITURA_INTERVALO_MS) {
    ultimaLeituraMs = millis();
    enviarLeitura();
  }
}

// ================= Leitura dos sensores =================

float lerTemperatura() {
  sensorTemperatura.requestTemperatures();
  esperar(800); // conversão do DS18B20 (12 bits = ~750 ms), olhando o botão enquanto espera
  float t = sensorTemperatura.getTempCByIndex(0);
  if (t == DEVICE_DISCONNECTED_C) {
    // DS18B20 não respondeu (o motor da bomba derruba a leitura de vez em quando):
    // segura o último valor bom por algumas leituras antes de dar o sensor por perdido
    if (temperaturaJaLida && falhasTemperatura < FALHAS_TEMPERATURA_TOLERADAS) {
      falhasTemperatura++;
      return ultimaTemperaturaValida;
    }
    return -127.0; // valor de erro padrão da DallasTemperature (sensor sumiu de verdade)
  }
  falhasTemperatura = 0;
  temperaturaJaLida = true;
  ultimaTemperaturaValida = t;
  return t;
}

float lerTensaoMedia(uint8_t pino, uint8_t amostras) {
  // média de várias leituras para reduzir ruído do ADC
  uint32_t soma = 0;
  for (uint8_t i = 0; i < amostras; i++) {
    soma += analogRead(pino);
    esperar(5);
  }
  float media = (float)soma / amostras;
  return (media / ADC_VALOR_MAXIMO) * TENSAO_REFERENCIA;
}

float lerTDS(float temperaturaC) {
  float tensao = lerTensaoMedia(PINO_TDS, 10);

  // sem temperatura válida (-127) a compensação zerava o TDS: usa 25 C (sem compensação)
  if (temperaturaC == -127.0) temperaturaC = 25.0;

  // compensação de temperatura (referência 25°C) — fórmula padrão
  // usada pelos módulos TDS Meter v1.0 / DFRobot
  float coeficienteCompensacao = 1.0 + 0.02 * (temperaturaC - 25.0);
  float tensaoCompensada = tensao / coeficienteCompensacao;

  float tds = (133.42 * pow(tensaoCompensada, 3)
             - 255.86 * pow(tensaoCompensada, 2)
             + 857.39 * tensaoCompensada) * 0.5;

  if (tds < 0) tds = 0;
  return tds; // em ppm
}

float lerTurbidez() {
  float tensao = lerTensaoMedia(PINO_TURBIDEZ, 10);

  // TODO: substituir por curva real depois de calibrar (constantes no topo)
  // por enquanto: escala linear simples entre as duas tensões de referência
  float faixa = TURBIDEZ_TENSAO_CLARA - TURBIDEZ_TENSAO_TURVA;
  float turbidezEstimativa = (TURBIDEZ_TENSAO_CLARA - tensao) / faixa * 100.0;
  if (turbidezEstimativa < 0) turbidezEstimativa = 0;
  return turbidezEstimativa; // unidade provisória, recalibrar
}

// ================= Comunicação serial =================

// Devolve o nome do sensor que passou do limite, ou nullptr se está tudo dentro.
// Temperatura -127 = DS18B20 sem resposta: não conta como leitura, não corta.
const char* sensorForaDoLimite(float tds, float turbidez, float temperatura) {
  if (tds > limiteTdsPpm) return "TDS";
  if (turbidez > limiteTurbidez) return "TURBIDEZ";
  if (temperatura != -127.0 && temperatura > limiteTemperaturaC) return "TEMPERATURA";
  return nullptr;
}

void enviarLeitura() {
  float temperatura = lerTemperatura();
  float tds = lerTDS(temperatura);
  float turbidez = lerTurbidez();

  // Proteção: corta a bomba ANTES de imprimir, o mais rápido possível.
  const char* sensorFora = sensorForaDoLimite(tds, turbidez, temperatura);
  acimaDoLimite = (sensorFora != nullptr);
  bool cortouAgora = false;
  if (acimaDoLimite && bombaLigada) {
    releDesligar();
    cortouAgora = true;
  }

  Serial.print("L,");
  Serial.print(tds, 2);
  Serial.print(",");
  Serial.print(turbidez, 2);
  Serial.print(",");
  Serial.print(temperatura, 2);
  Serial.print("\n");

  if (cortouAgora) {
    Serial.print("E,LIMITE,");
    Serial.print(sensorFora);
    Serial.print("\n");
  }
}

void processarComandosSerial() {
  static bool descartandoLinha = false; // linha gigante: joga fora até o próximo '\n'
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n') {
      if (!descartandoLinha) {
        comandoRecebido.trim();
        executarComando(comandoRecebido);
      }
      comandoRecebido = "";
      descartandoLinha = false;
    } else if (!descartandoLinha) {
      if (comandoRecebido.length() >= TAMANHO_MAX_COMANDO) {
        comandoRecebido = "";
        descartandoLinha = true;
      } else {
        comandoRecebido += c;
      }
    }
  }
}

// ================= Handshake e negociação de baud =================

bool baudSuportado(uint32_t baud) {
  for (uint8_t i = 0; i < NUM_BAUDS; i++) {
    if (BAUDS_SUPORTADOS[i] == baud) return true;
  }
  return false;
}

// Só dígitos (1 a 7): "115200". Recusa "", "-1", "96.0", "1e5", "9 600".
bool lerBaud(const String& texto, uint32_t& baud) {
  if (texto.length() == 0 || texto.length() > 7) return false;
  uint32_t valor = 0;
  for (unsigned int i = 0; i < texto.length(); i++) {
    char c = texto[i];
    if (c < '0' || c > '9') return false;
    valor = valor * 10 + (uint32_t)(c - '0');
  }
  baud = valor;
  return true;
}

// Troca o baud da conversa. Na USB CDC o baud é virtual (só guardamos o número); na UART real reconfigura.
void aplicarBaud(uint32_t baud) {
#if !SERIAL_E_USB_CDC
  Serial.flush();    // a resposta (H,BAUD) sai inteira no baud antigo antes da troca
  Serial.begin(baud);
#endif
  baudAtual = baud;
}

// AGUARDANDO: calada, a 9600, bomba desligada (sem supervisão válida = estado seguro).
void entrarEmEspera() {
  telemetriaAtiva = false;
  releDesligar();
  if (baudAtual != BAUD_INICIAL) aplicarBaud(BAUD_INICIAL);
}

// O PC pediu BAUD mas sumiu antes do START: volta sozinha para 9600 para o próximo HELLO ser ouvido.
void verificarTempoDoHandshake() {
  if (!telemetriaAtiva && baudAtual != BAUD_INICIAL && millis() - baudTrocadoMs >= HANDSHAKE_TIMEOUT_MS) {
    entrarEmEspera();
  }
}

void responderHello() {
  Serial.print("H,READY,");
  Serial.print(FW_VERSAO);
  Serial.print(",");
  for (uint8_t i = 0; i < NUM_BAUDS; i++) {
    if (i > 0) Serial.print(";");
    Serial.print(BAUDS_SUPORTADOS[i]);
  }
  Serial.print("\n");
  entrarEmEspera(); // HELLO reinicia o enlace: AGUARDANDO a 9600, bomba desligada
}

void executarComandoBaud(const String& comando) {
  if (telemetriaAtiva) {
    Serial.print("H,ERRO,ESTADO\n"); // trocar o baud no meio da telemetria quebraria a conversa: faça HELLO antes
    return;
  }
  uint32_t baud = 0;
  if (!lerBaud(comando.substring(5), baud) || !baudSuportado(baud)) {
    Serial.print("H,ERRO,BAUD\n");
    return;
  }
  Serial.print("H,BAUD,");
  Serial.print(baud);
  Serial.print("\n");
  aplicarBaud(baud);
  baudTrocadoMs = millis();
}

void iniciarTelemetria() {
  if (!telemetriaAtiva) {
    telemetriaAtiva = true;
    ultimaLeituraMs = millis() - LEITURA_INTERVALO_MS; // a primeira leitura sai já
  }
  Serial.print("H,START\n"); // repetir o START é inofensivo: só confirma de novo
}

// Só dígitos e no máximo um ponto, com pelo menos um dígito ("200", "185.5").
// Recusa "abc", "12abc", "1e9", "-5", "1.2.3", "" e " ": o toFloat() sozinho aceitaria lixo.
bool numeroValido(const String& texto) {
  bool temDigito = false;
  bool temPonto = false;
  for (unsigned int i = 0; i < texto.length(); i++) {
    char c = texto[i];
    if (c >= '0' && c <= '9') {
      temDigito = true;
    } else if (c == '.' && !temPonto) {
      temPonto = true;
    } else {
      return false;
    }
  }
  return temDigito;
}

// LIMITE,<TDS|TURBIDEZ|TEMPERATURA>,<valor>  ->  muda o limite e confirma com A,LIMITE,<sensor>,<valor>.
// Qualquer defeito (formato, sensor, número, faixa): ignora sem confirmar; o SCADA avisa por timeout.
void executarComandoLimite(const String& comando) {
  int p1 = comando.indexOf(',');
  int p2 = comando.indexOf(',', p1 + 1);
  if (p1 < 0 || p2 < 0) return;

  String sensor = comando.substring(p1 + 1, p2);
  String texto  = comando.substring(p2 + 1);
  if (!numeroValido(texto)) return;
  float valor = texto.toFloat();
  if (valor <= 0.0 || valor > LIMITE_MAXIMO_ACEITO) return;

  const char* nome;
  if (sensor == "TDS")              { limiteTdsPpm = valor;       nome = "TDS"; }
  else if (sensor == "TURBIDEZ")    { limiteTurbidez = valor;     nome = "TURBIDEZ"; }
  else if (sensor == "TEMPERATURA") { limiteTemperaturaC = valor; nome = "TEMPERATURA"; }
  else return;

  Serial.print("A,LIMITE,");
  Serial.print(nome);
  Serial.print(",");
  Serial.print(valor, 2);
  Serial.print("\n");
}

void executarComando(String comando) {
  if (comando == "HELLO") {
    responderHello();
  } else if (comando.startsWith("BAUD,")) {
    executarComandoBaud(comando);
  } else if (comando == "START") {
    iniciarTelemetria();
  } else if (comando == "RELE_ON") {
    if (!telemetriaAtiva) {
      Serial.print("H,ERRO,ESTADO\n"); // sem handshake a bomba não liga
    } else if (acimaDoLimite || botaoPressionado) {
      Serial.print("E,BLOQUEADO\n"); // leitura acima do limite ou botão apertado: não liga a bomba
    } else {
      releLigar();
      Serial.print("A,RELE_ON\n");
    }
  } else if (comando == "RELE_OFF") {
    releDesligar();
    Serial.print("A,RELE_OFF\n");
  } else if (comando.startsWith("LIMITE,")) {
    if (telemetriaAtiva) {
      executarComandoLimite(comando);
    } else {
      Serial.print("H,ERRO,ESTADO\n");
    }
  }
  // qualquer outro comando é ignorado silenciosamente
}
