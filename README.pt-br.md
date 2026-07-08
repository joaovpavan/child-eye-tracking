*[Read this in English](README.md)*

# Eye Tracking Obiquos

Um projeto de óculos de eye-tracking DIY (faça você mesmo), construído com
duas câmeras ESP32 e um pipeline de pós-processamento em Python. Uma câmera
fica voltada para o olho, a outra para frente; ambas transmitem vídeo MJPEG
via Wi-Fi para um PC, que estima para onde o olho está olhando e mapeia esse
olhar sobre a visão da câmera frontal em tempo real.

## Visão Geral

Eye trackers comerciais são caros e fechados. Este projeto explora uma
alternativa de baixo custo: dois módulos de câmera ESP32 prontos, montados
em uma armação de óculos, transmitindo para um PC através de uma rede Wi-Fi
própria, com toda a estimativa de olhar, calibração e gravação feitas em
Python no lado do PC — sem nuvem, sem hardware proprietário.

### Pipeline

```
┌──────────────┐      ┌──────────────┐       ┌──────────────────────┐
│  Câmera do   │      │   Câmera     │       │  Pós-processamento   │
│  olho        │─────▶│   frontal    │─────▶│   no PC              │
│  (ESP32,     │ Wi-Fi│ (ESP32-S3,   │ Wi-Fi │ • Detecção da pupila │
│  unidade STA)│      │ unidade AP)  │       │ • Calibração         │
└──────────────┘      └──────────────┘       │ • Mapeamento olhar → │
                                             │   ângulo frontal     │
                                             │ • Visualizador web + │
                                             │   gravação           │
                                             └──────────────────────┘
```

O ESP32 da câmera frontal também funciona como o ponto de acesso Wi-Fi ao
qual a câmera do olho se conecta, então o par forma uma rede autocontida,
sem necessidade de roteador — basta o PC entrar nesse mesmo hotspot.

## Principais Funcionalidades

- **Transmissão MJPEG dupla** de duas placas ESP32 via um hotspot Wi-Fi privado
- **Estimativa de olhar baseada na pupila**, mapeando o ângulo da câmera do
  olho para um ponto na visão da câmera frontal
- **Calibração multiponto**, com perfis salvos separadamente para usuários
  `child` (criança) e `adult` (adulto) (diferentes padrões de geometria/FOV)
- **Visualizador web de stream duplo** (Flask) com overlay de olhar ao vivo,
  gravação de sessão e rastreamento de placa/alvo para validar a precisão
- **Validação pós-sessão** — compara o olhar previsto com um alvo conhecido
  para quantificar a precisão depois da sessão

## Estrutura do Repositório

```
firmware/
├── esp32_eye_camera/        Firmware ESP-IDF da câmera voltada para o olho (STA)
└── esp32s3_forward_camera/  Firmware ESP-IDF da câmera frontal (AP)

postprocessing/
├── dual_stream_viewer.py    Visualizador lado a lado dos dois streams MJPEG
├── dual_stream_web.py       Visualizador/gravador web (Flask) — ponto de entrada principal
├── eye_forward_alignment.py Estimativa de olhar + calibração + overlay
├── plate_tracker.py         Rastreamento de placa/alvo
├── mjpeg_stream.py          Leitor de stream MJPEG compartilhado
└── validate_session.py      Validação de sessão/gravação

run.bat                      Setup + execução em um único comando (Windows)
```

Veja [`firmware/esp32_eye_camera/README.md`](firmware/esp32_eye_camera/README.md) e
[`firmware/esp32s3_forward_camera/README.md`](firmware/esp32s3_forward_camera/README.md)
para compilar e gravar cada placa, e
[`postprocessing/README.md`](postprocessing/README.md) para o pipeline do lado do PC.

## Começando

Veja [SETUP.md](SETUP.md) *(em inglês)* para as instruções completas de
configuração do ambiente, gravação das placas e execução. Se as placas já
estiverem gravadas e conectadas à rede Wi-Fi, usuários Windows podem apenas
rodar o `run.bat`, que configura o ambiente Python e abre o visualizador web
em uma única etapa.

Versão rápida, do zero:

```bash
# Lado Python
setup_venv.bat                     # ou: python -m venv venv && pip install -r requirements.txt
cp .env.example .env               # depois preencha suas URLs de stream/perfil/porta

# Lado firmware (por placa, a partir da sua própria pasta firmware/)
cp sdkconfig.local.example sdkconfig.local   # depois preencha seu SSID/senha do Wi-Fi
idf.py build flash monitor
```

**Antes de gravar em hardware real**, troque as credenciais de Wi-Fi padrão —
o `main/Kconfig.projbuild` de cada firmware vem com uma senha placeholder
(`changeme123`) que não deve permanecer em dispositivos implantados. Defina
a sua própria via `sdkconfig.local` (ignorado pelo git, mesclado
automaticamente) em vez de editar o `sdkconfig.defaults` versionado.

## Limitações Conhecidas

- **Os IPs dos streams assumem uma rede nova e dedicada.** A câmera frontal
  (AP) é sempre `192.168.4.1`; a câmera do olho (STA) recebe `192.168.4.2`
  como primeiro e único cliente DHCP. Isso é confiável na prática, mas não
  garantido — ambos os firmwares também anunciam hostnames mDNS
  (`forward-cam.local` / `eye-cam.local`) como alternativa, embora o Windows
  puro nem sempre resolva nomes `.local` sem software adicional.
- **Apenas um rádio Wi-Fi por vez.** Conectar-se ao hotspot dos óculos faz o
  PC perder sua conexão normal com a internet durante a sessão.
- **A calibração é por perfil, não por usuário.** Os perfis `child`/`adult`
  definem suposições padrão de geometria; a precisão ainda depende de rodar
  a calibração multiponto para quem está usando os óculos de fato.

## Dependências

Veja [`requirements.txt`](requirements.txt) para a lista completa de dependências Python.

## Contribuindo

Issues e pull requests são bem-vindos. Para mudanças de firmware, teste em
hardware real antes de enviar — o QEMU em `.devcontainer/` cobre a
verificação de build, mas não o comportamento de câmera/Wi-Fi.

## Licença

MIT — veja [LICENSE](LICENSE). Isso cobre o código deste repositório; não
relicencia dependências de terceiros (ex.: componentes gerenciados do
ESP-IDF), que mantêm suas próprias licenças.
