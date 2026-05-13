# Cerebras Inference API — Referencia para FreeAI

> **Fecha de captura:** 2026-05-13
> **Propósito:** Documento de referencia exhaustivo de la API de Cerebras Inference, con énfasis en la **capa gratuita**, pensado para alimentar una skill futura que integre Cerebras como provider en FreeAI.
> **Estado:** sólo documentación. No hay código de integración en el repo todavía.

---

## 1. Resumen ejecutivo

**Cerebras Inference** es un servicio de inferencia LLM operado por Cerebras Systems sobre sus propios chips Wafer-Scale Engine (WSE). El argumento de venta principal es velocidad: throughputs de **1.000 a 3.000 tokens/segundo** según modelo, frente a los 50–200 t/s típicos de proveedores GPU.

### Por qué interesa a FreeAI

1. **Free tier real, no trial.** 1.000.000 de tokens/día por modelo, **sin caducidad y sin tarjeta de crédito**. Esto encaja con la propuesta de FreeAI de exponer un orquestador "free-first" a usuarios sin claves propias.
2. **Compatibilidad con SDK de OpenAI.** El endpoint habla el mismo protocolo, así que se puede integrar reutilizando el `OpenAICompatibleProvider` existente en el repo.
3. **Modelos abiertos competitivos.** `gpt-oss-120b` (OpenAI open-source), `qwen-3-235b`, `zai-glm-4.7`. Razonamiento, tool calling y structured output soportados.

### Lo que hay que vigilar

- Algunos modelos están en **Preview** (Cerebras los marca como "no production-ready").
- `llama3.1-8b` y `qwen-3-235b-a22b-instruct-2507` **se deprecan el 2026-05-27**.
- `zai-glm-4.7` tiene un rate limit en free **mucho más restrictivo** (10 RPM vs 30 RPM).

---

## 2. Autenticación y endpoint

| Campo | Valor |
|---|---|
| Portal | https://cloud.cerebras.ai → "API Keys" |
| Variable de entorno recomendada | `CEREBRAS_API_KEY` |
| Base URL | `https://api.cerebras.ai/v1` |
| Cabecera de auth | `Authorization: Bearer <api-key>` |
| Content-Type | `application/json` (también soporta `application/vnd.msgpack`) |
| Content-Encoding | opcional `gzip` |

Set rápido en local:

```bash
# macOS / Linux
export CEREBRAS_API_KEY="csk-..."

# Windows (PowerShell)
$env:CEREBRAS_API_KEY = "csk-..."

# Windows (persistente)
setx CEREBRAS_API_KEY "csk-..."
```

---

## 3. Modelos soportados

| Model ID | Parámetros | Context window | Throughput | Estado | Notas |
|---|---|---|---|---|---|
| `gpt-oss-120b` | 120 B | 131 K | ~3.000 t/s | **Production** | Modelo open-source de OpenAI. Recomendado como default. |
| `llama3.1-8b` | 8 B | 128 K (8 K en free) | ~2.200 t/s | Production | ⚠️ **Deprecación 2026-05-27.** No usar para producción nueva. |
| `qwen-3-235b-a22b-instruct-2507` | 235 B (22 B activos) | 131 K | ~1.400 t/s | **Preview** | ⚠️ **Deprecación 2026-05-27.** |
| `zai-glm-4.7` | 355 B | 131 K | ~1.000 t/s | **Preview** | Razonamiento. Rate limit reducido en free (10 RPM). |

> Cerebras advierte explícitamente que **los modelos en Preview no están pensados para producción**. Para FreeAI conviene marcarlos con un flag `preview: true` en el catálogo de modelos para no exponerlos por defecto.

**Otras familias** (Llama 3.3 70B, Llama 4 Scout, DeepSeek R1) aparecen mencionadas en blogs/marketing y en algunos endpoints dedicados, pero la lista oficial de modelos auto-servicio actual (2026-05-13) son los cuatro de arriba.

---

## 4. Pricing y tiers

### 4.1 Free tier — el que nos importa

- **1 M tokens/día por modelo**, sin caducidad, sin tarjeta.
- **Context length por defecto: 8.192 tokens** (ampliable a 128 K bajo solicitud al soporte).

Rate limits exactos (TPM = tokens/min, TPH = tokens/h, TPD = tokens/día, RPM/RPH/RPD análogos para requests):

| Model | TPM | TPH | TPD | RPM | RPH | RPD |
|---|---|---|---|---|---|---|
| `gpt-oss-120b` | 64 K | 1 M | 1 M | 30 | 900 | 14.400 |
| `llama3.1-8b` | 60 K | 1 M | 1 M | 30 | 900 | 14.400 |
| `qwen-3-235b-a22b-instruct-2507` | 60 K | 1 M | 1 M | 30 | 900 | 14.400 |
| `zai-glm-4.7` | 60 K | 1 M | 1 M | **10** | **100** | **100** |

> ⚠️ `zai-glm-4.7` en free es prácticamente solo para experimentación: 100 requests/día totales.

### 4.2 Developer (Pay-as-you-go)

- Auto-servicio desde **$10**.
- ~10× los rate limits del free tier.
- **Sin restricciones horarias o diarias** — sólo TPM/RPM.

| Model | TPM | RPM |
|---|---|---|
| `gpt-oss-120b` | 1 M | 1 K |
| `llama3.1-8b` | 2 M | 2 K |
| `qwen-3-235b-a22b-instruct-2507` | 500 K | 500 |
| `zai-glm-4.7` | 500 K | 500 |

Precios blended (input+output, ratio 3:1 según Artificial Analysis): **rango $0.10 – $2.38 por 1 M tokens** dependiendo de tamaño de modelo. Cerebras no publica una tabla pública input/output desglosada en su pricing page; consultar la API de billing o el dashboard.

### 4.3 Enterprise

- Cola dedicada, soporte con SLA, fine-tuning, pesos custom.
- Rate limits negociados.
- Fuera del scope inicial de FreeAI.

### 4.4 Otros productos (no API)

- **Cerebras Code Pro** ($50/mes, 24 M tokens/día) y **Max** ($200/mes, 120 M tokens/día) — suscripción para coding tools. **Sold out** en 2026-05-13. No es la API que integraremos.

---

## 5. API: Chat Completions

**Endpoint:** `POST https://api.cerebras.ai/v1/chat/completions`

Compatible con el wire format de OpenAI Chat Completions. La gran mayoría del código escrito contra OpenAI funciona cambiando solo `base_url` y la clave.

### 5.1 Parámetros (request)

#### Obligatorios

- `model` *(string)* — uno de los IDs de la sección 3.
- `messages` *(array)* — historial de la conversación.

#### Sampling / generación

| Parámetro | Tipo | Rango | Notas |
|---|---|---|---|
| `temperature` | float | 0 – 2.0 | |
| `top_p` | float | 0 – 1 | nucleus sampling |
| `frequency_penalty` | float | -2.0 – 2.0 | |
| `presence_penalty` | float | -2.0 – 2.0 | |
| `max_completion_tokens` | int | — | Cuenta para estimación de rate limit. |
| `seed` | int | — | Sampling determinista. |
| `stop` | string[] | hasta 4 | |

#### Tool use

| Parámetro | Tipo | Notas |
|---|---|---|
| `tools` | array | Definiciones de funciones. |
| `tool_choice` | `"none"` \| `"auto"` \| `"required"` \| objeto | |
| `parallel_tool_calls` | bool | default `true` |

#### Formato de salida

- `response_format`:
  - `{"type": "text"}` (default)
  - `{"type": "json_object"}` (legacy JSON mode)
  - `{"type": "json_schema", "json_schema": {...}}` (structured outputs)

#### Razonamiento

- `reasoning_effort`: `"low"` | `"medium"` | `"high"` | `"none"` — sólo modelos de razonamiento.
- `clear_thinking` (sólo `zai-glm-4.7`): preserva el reasoning de turnos previos. **No estándar OpenAI** → pasar por `extra_body` si se usa el SDK de OpenAI.

#### Control de petición

- `stream` *(bool)* — habilita SSE con deltas parciales.
- `service_tier`: `"priority"` | `"default"` | `"auto"` | `"flex"` (private preview).
- `queue_threshold` *(int)* — límite de tiempo en cola para flex/auto (private preview).

#### Caching / observabilidad

- `prompt_cache_key` *(string)* — agrupa requests para prompt caching.
- `prediction` — output predicho para acelerar generación.
- `logprobs` *(bool)*, `top_logprobs` *(int)*.

### 5.2 Roles soportados

`system`, `user`, `assistant`, `developer`, `tool`.

> En `gpt-oss-120b`, `system` y `developer` **se mapean al mismo nivel**. Mismo prompt puede dar comportamiento distinto al de OpenAI por esto.

### 5.3 Response schema

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1715600000,
  "model": "gpt-oss-120b",
  "system_fingerprint": "fp_...",
  "service_tier_used": "default",
  "choices": [
    {
      "index": 0,
      "finish_reason": "stop",
      "message": {
        "role": "assistant",
        "content": "...",
        "reasoning": "...",          // sólo modelos de razonamiento
        "tool_calls": [              // sólo si hay tool use
          {
            "id": "call_...",
            "type": "function",
            "function": { "name": "...", "arguments": "<json string>" }
          }
        ]
      }
    }
  ],
  "usage": {
    "prompt_tokens": 42,
    "completion_tokens": 128,
    "total_tokens": 170,
    "prompt_tokens_details": { "cached_tokens": 0 }
  },
  "time_info": {
    "queue_time": 0.001,
    "prompt_time": 0.010,
    "completion_time": 0.045,
    "total_time": 0.056
  }
}
```

### 5.4 Streaming

Con `stream: true`, la respuesta llega como **Server-Sent Events** con objetos `chat.completion.chunk` cuyo `delta` contiene contenido parcial, terminando con `data: [DONE]`. Idéntico al protocolo de OpenAI.

---

## 6. Rate limits — cómo leerlos en vivo

### 6.1 Cómo se calculan

> *"Mide uso por requests enviadas y tokens usados dentro de un timeframe. El primer threshold que se alcance — request o token — dispara el límite."*

Estimación de tokens por petición:

```
tokens_estimados = input_tokens + (max_completion_tokens
                                   if max_completion_tokens is set
                                   else max_sequence_length - input_tokens)
```

Esto implica que **no fijar `max_completion_tokens` quema cuota** porque Cerebras asume el peor caso.

### 6.2 Headers de respuesta

Cada respuesta incluye estos headers (visibles con `curl --verbose`):

- `x-ratelimit-remaining-tokens-minute`
- `x-ratelimit-remaining-requests-day`
- `x-ratelimit-reset-tokens-minute` (segundos hasta reset)
- `x-ratelimit-reset-requests-day` (segundos hasta reset)

Para FreeAI conviene loguear estos headers por request y exponerlos en el dashboard de uso.

---

## 7. Manejo de errores

> Nota: la página oficial de errores de Cerebras (`/api-reference/errors`, `/support/errors`) devolvía 404 al capturar este documento. Asumir convención OpenAI-compatible:

| Código | Significado | Acción recomendada |
|---|---|---|
| 400 | Body mal formado | No reintentar. Loguear payload. |
| 401 | API key inválida o ausente | No reintentar. Surface al usuario. |
| 403 | Sin permiso para el modelo (tier/preview) | No reintentar. Sugerir upgrade o cambiar modelo. |
| 404 | Modelo no existe | No reintentar. Validar `model` contra catálogo. |
| 422 | Validación de parámetros | No reintentar. Corregir request. |
| 429 | Rate limit | **Reintentar** respetando `retry-after` y `x-ratelimit-reset-*`. |
| 500 / 502 / 503 / 504 | Error transitorio | Backoff exponencial con jitter. Máx 3 reintentos. |

**Estrategia para FreeAI**

- Backoff exponencial con jitter: `base = 1s`, `max = 30s`, `factor = 2`, jitter `±20%`.
- Máximo 3 reintentos para 429/5xx.
- Para 429, leer `x-ratelimit-reset-tokens-minute` y esperar ese tiempo si es menor que el backoff calculado.
- 401/403/404/422 → propagar al cliente, no reintentar.

---

## 8. Ejemplos

### 8.1 cURL básico

```bash
curl https://api.cerebras.ai/v1/chat/completions \
  -H "Authorization: Bearer $CEREBRAS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-oss-120b",
    "messages": [
      {"role": "user", "content": "Why is fast inference important?"}
    ],
    "max_completion_tokens": 200
  }'
```

### 8.2 Python — SDK oficial

```bash
pip install --upgrade cerebras_cloud_sdk
```

```python
import os
from cerebras.cloud.sdk import Cerebras

client = Cerebras(api_key=os.environ["CEREBRAS_API_KEY"])

resp = client.chat.completions.create(
    model="gpt-oss-120b",
    messages=[{"role": "user", "content": "Why is fast inference important?"}],
    max_completion_tokens=200,
)
print(resp.choices[0].message.content)
```

### 8.3 Python — SDK de OpenAI (drop-in, el más relevante para FreeAI)

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://api.cerebras.ai/v1",
    api_key=os.environ["CEREBRAS_API_KEY"],
)

resp = client.chat.completions.create(
    model="gpt-oss-120b",
    messages=[{"role": "user", "content": "Hola"}],
    max_completion_tokens=200,
)
print(resp.choices[0].message.content)
```

Para parámetros no estándar (e.g. `clear_thinking`), usar `extra_body`:

```python
resp = client.chat.completions.create(
    model="zai-glm-4.7",
    messages=[...],
    reasoning_effort="none",
    extra_body={"clear_thinking": False},
)
```

### 8.4 Streaming

```python
stream = client.chat.completions.create(
    model="gpt-oss-120b",
    messages=[{"role": "user", "content": "Cuenta hasta 5"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content or ""
    print(delta, end="", flush=True)
```

### 8.5 Tool calling

```python
tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]

resp = client.chat.completions.create(
    model="gpt-oss-120b",
    messages=[{"role": "user", "content": "¿Qué tiempo hace en Madrid?"}],
    tools=tools,
    tool_choice="auto",
)
tool_calls = resp.choices[0].message.tool_calls
```

### 8.6 Structured output (JSON schema)

```python
resp = client.chat.completions.create(
    model="gpt-oss-120b",
    messages=[{"role": "user", "content": "Dame un usuario de ejemplo"}],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "User",
            "schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "age":  {"type": "integer"},
                },
                "required": ["name", "age"],
            },
        },
    },
)
```

### 8.7 Lectura de headers de rate limit (httpx)

```python
import httpx, os

r = httpx.post(
    "https://api.cerebras.ai/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {os.environ['CEREBRAS_API_KEY']}",
        "Content-Type": "application/json",
    },
    json={
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 10,
    },
    timeout=30,
)
print("remaining tokens/min:", r.headers.get("x-ratelimit-remaining-tokens-minute"))
print("remaining req/day:",    r.headers.get("x-ratelimit-remaining-requests-day"))
print("reset tokens/min (s):", r.headers.get("x-ratelimit-reset-tokens-minute"))
```

---

## 9. Integración en FreeAI — mapa para la skill

### 9.1 Patrón de provider en el repo

- **Base abstracta:** [backend/app/providers/base.py](../../backend/app/providers/base.py) (`BaseProvider`).
- **Helper OpenAI-compatible:** existe `OpenAICompatibleProvider` en `backend/app/providers/`. Cerebras habla wire format de OpenAI → **hereda de ahí**, no de `BaseProvider`.
- **Registry:** [backend/app/providers/__init__.py](../../backend/app/providers/__init__.py) (`PROVIDER_REGISTRY`).
- **Catálogo por defecto:** [backend/app/repositories/config_repo.py](../../backend/app/repositories/config_repo.py) (`DEFAULT_PROVIDERS`).
- **Modelos conocidos:** [backend/app/providers/known_models.py](../../backend/app/providers/known_models.py).
- **Cifrado de keys:** [backend/app/crypto.py](../../backend/app/crypto.py) (Fernet, automático).

### 9.2 Pasos para añadir Cerebras

| Paso | Archivo | Qué hacer |
|---|---|---|
| a | `backend/app/providers/cerebras_provider.py` *(nuevo)* | Subclase de `OpenAICompatibleProvider`. `name = "cerebras"`. `BASE_URL = "https://api.cerebras.ai/v1"`. `supports_streaming = True`, `supports_tools = True`, `supports_vision = False`. |
| b | `backend/app/providers/__init__.py` | Añadir entrada en `PROVIDER_REGISTRY`. |
| c | `backend/app/repositories/config_repo.py` | Añadir Cerebras a `DEFAULT_PROVIDERS` con metadata (display name, docs URL, key format `csk-...`). |
| d | `backend/app/providers/known_models.py` | Registrar los 4 modelos con `context_window`, `max_output`, capabilities, y flags `preview` / `deprecation_date` cuando aplique. |
| e | `backend/app/settings.py` *(opcional)* | Leer `CEREBRAS_API_KEY` del entorno para seed inicial. |

### 9.3 Consideraciones específicas de FreeAI

- **Cifrado de keys:** automático si se sigue el patrón. Las keys viven en `user_providers`, cifradas con Fernet, con override COALESCE catálogo→usuario.
- **Default free-friendly:** el free tier de Cerebras (1 M tokens/día, sin tarjeta) es **excelente** como provider por defecto para usuarios anónimos o sin clave propia. Considerar `gpt-oss-120b` como `default_model` del provider.
- **Preview / deprecación:**
  - Marcar `qwen-3-235b-a22b-instruct-2507` y `zai-glm-4.7` con `preview: true` → no exponer por defecto en UI.
  - `llama3.1-8b` y `qwen-3-235b-a22b-instruct-2507`: setear `deprecation_date: "2026-05-27"`. La UI puede mostrar warning.
- **Rate limit de `zai-glm-4.7` en free:** sólo 10 RPM / 100 RPD. Si se expone, el cliente del orquestador debe respetar un rate-limiter dedicado para este modelo (más estricto que el de los otros tres).
- **`max_completion_tokens` siempre:** el algoritmo de cuota de Cerebras asume worst-case si no se fija. El orquestador debe inyectar un default razonable (p. ej. 4096) para no quemar la cuota gratuita.
- **Headers de rate limit:** el provider debería loguear `x-ratelimit-remaining-*` en cada llamada y emitirlo a métricas para el dashboard de uso.

### 9.4 Verificación cuando se implemente

1. **Test unitario:** mock del cliente OpenAI, verificar que `complete()` devuelve `ProviderResponse` válido y que `stream()` produce chunks.
2. **Test integración real (opcional, requiere key):** `CEREBRAS_API_KEY=... pytest -k cerebras_live` haciendo una llamada real a `gpt-oss-120b` con `max_completion_tokens=10`.
3. **Verificación de headers:** confirmar lectura de `x-ratelimit-*` y exposición a logs/metrics.
4. **End-to-end por UI:** dar de alta una key Cerebras en la pestaña Providers, lanzar un chat contra `gpt-oss-120b` y verificar que el token usage se registra correctamente.

---

## 10. Apéndice

### 10.1 Limitaciones documentadas

- Modelos en Preview no soportan producción según Cerebras.
- En `gpt-oss-120b`, roles `system` y `developer` se colapsan (difiere de OpenAI).
- Algunos parámetros (`clear_thinking`, `service_tier`, `queue_threshold`) no están en el SDK estándar de OpenAI → usar `extra_body` o el SDK nativo `cerebras_cloud_sdk`.
- Suscripciones Cerebras Code (Pro/Max) están **sold out** en 2026-05-13 y son producto distinto a la API self-serve.
- Hourly/daily limits **sólo aplican al free tier**; el tier Developer sólo enfrenta TPM/RPM.

### 10.2 Fuentes consultadas (capturadas 2026-05-13)

- https://inference-docs.cerebras.ai/introduction
- https://inference-docs.cerebras.ai/quickstart
- https://inference-docs.cerebras.ai/models/overview
- https://inference-docs.cerebras.ai/support/rate-limits
- https://inference-docs.cerebras.ai/api-reference/chat-completions
- https://inference-docs.cerebras.ai/resources/openai
- https://www.cerebras.ai/pricing
- https://www.cerebras.ai/inference
- https://pricepertoken.com/endpoints/cerebras/free
- https://artificialanalysis.ai/providers/cerebras
- https://tokenmix.ai/blog/cerebras-api-key-rate-limits-free-tier-2026
- https://tokenmix.ai/blog/cerebras-api-key-access-speed-tests-2026
- https://support.cerebras.net/articles/9996007307-cerebras-code-faq
