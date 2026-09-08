# Controle de Uniformes por Visão Computacional e IoT

Projeto desenvolvido para a avaliação prática de Internet das Coisas. A aplicação usa uma câmera USB ou vídeo, detecta pessoas com YOLOv8, classifica a região do tronco por faixas HSV, conta cada pessoa uma vez por dia e publica eventos de não conformidade.

## Requisitos

- Python 3.10 ou superior.
- Câmera USB ou arquivo de vídeo.
- `yolov8n.pt` no diretório do projeto.
- Para alertas MQTT: um broker acessível e `paho-mqtt` instalado.
- Para webhook compatível com WhatsApp/gateway: `requests` instalado e endpoint configurado.

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Execução

Câmera USB padrão:

```bash
python contador_uniformes.py --source 0 --model yolov8n.pt
```

Vídeo para teste:

```bash
python contador_uniformes.py --source WIN_20260908_14_47_59_Pro.mp4 --model yolov8n.pt 
#video do Marcos testando

A tecla `Q` ou `ESC` encerra. O arquivo `eventos.jsonl` registra eventos em formato estruturado.

## Comunicação IoT

A aplicação funciona localmente mesmo sem mensageria. Para MQTT, defina as variáveis antes de iniciar:

```bash
export MQTT_HOST=192.168.0.10
export MQTT_PORT=1883
export MQTT_TOPIC=senai/corredor/alertas
```

Para um gateway de WhatsApp ou endpoint HTTP autorizado:

```bash
export ALERT_WEBHOOK_URL=https://seu-gateway.example/alerta
```

O código está preparado para adicionar essas variáveis ao carregamento da configuração. O payload publicado contém `timestamp`, contadores, classificação e `track_id`. Nunca coloque tokens ou senhas no código; use variáveis de ambiente e o mecanismo de autenticação do gateway.

## Ajuste contra contagem duplicada

O rastreador mantém um ID mesmo quando o detector perde a pessoa por alguns frames. A associação usa a distância entre centroides e também o tamanho da caixa delimitadora. Os parâmetros principais são:

```python
max_match_distance: float = 110.0
max_missing_frames: int = 12
```

Se a mesma pessoa ainda for contada novamente, aumente `max_missing_frames` para `20` e, se o deslocamento for rápido, aumente `max_match_distance` para `140`. Se duas pessoas próximas estiverem recebendo o mesmo ID, reduza esses valores. A câmera deve permanecer fixa e a área de passagem deve ter boa iluminação.

## Critério de cor

A classificação considera o recorte central do tronco. O espaço HSV é usado porque separa matiz, saturação e brilho com maior tolerância a variações de iluminação do que a comparação direta em RGB. A configuração atual aceita o verde RGB `(121, 242, 178)` com a faixa HSV OpenCV `H 55–95, S 45–255, V 80–255`. Ajuste `uniform_hsv_ranges` e `min_uniform_ratio` em `contador_uniformes.py` após observar a iluminação real.

## Limitações conhecidas

O rastreamento é baseado em centroides e pode trocar identidades quando pessoas se cruzam. O resultado melhora com câmera fixa, corredor bem iluminado e distância suficiente entre pessoas. O contador é diário e é zerado automaticamente quando a data do sistema muda.
