# Laya Service — 接口文档

决策模型推理服务的 HTTP 接口说明。

| 项目 | 值 |
|---|---|
| 服务地址（本机） | `http://127.0.0.1:9800` |
| 服务地址（内网） | `http://<内网IP>:9800` |
| 服务地址（公网） | `http://<公网IP>:9800` — **需安全组放行 9800** |
| 交互式文档 | `http://<host>:9800/docs`（Swagger UI） |
| OpenAPI Schema | `http://<host>:9800/openapi.json` |
| 鉴权 | `Authorization: Bearer <LAYA_API_KEY>` |
| 内容类型 | `application/json` |
| 版本 | `0.1.0` |

> **English:** [API.en.md](API.en.md)。两份文档保持同步；若有出入，以英文版为准。

---

## 目录

- [快速开始](#快速开始)
- [鉴权](#鉴权)
- [统一响应格式](#统一响应格式)
- [错误码表](#错误码表)
- [接口详解](#接口详解)
  - [GET /healthz — 存活探针](#get-healthz--存活探针)
  - [GET /readyz — 就绪探针](#get-readyz--就绪探针)
  - [POST /v1/predict — 通用推理](#post-v1predict--通用推理)
  - [POST /v1/robot-dog/localization-reliability — 定位可信度](#post-v1robot-doglocalization-reliability--定位可信度)
- [调用示例](#调用示例)
- [关键概念：noul 不是布尔值](#关键概念noul-不是布尔值)
- [超时与性能](#超时与性能)
- [兼容层：OpenAI / Anthropic](#兼容层openai--anthropic)
- [API Key 管理](#api-key-管理)
- [常见问题](#常见问题)

---

> **服务器上的其他项目想调用？** 不用读这份文档。本机已装好 Claude Code
> skill `laya-call`，直接说「调用 laya 判断定位是否可靠」即可；也可以直接
> 复用现成客户端：
>
> ```python
> import sys; sys.path.insert(0, "<skill目录>/laya-call/scripts")
> from laya_client import LayaClient
> client = LayaClient.from_env()          # 自动读取端口和 API Key
> r = client.localization_reliability(
>     x_jump_m=2.3, y_stable=True, heading_reversals=3,
>     confidence_start=0.9, confidence_end=0.4, environment="indoor_weak_gps")
> if r.should_switch: switch_to_visual_imu()
> ```
>
> 客户端自带 503 重试、600 秒冷启动超时，以及把「不自信就保守回退」的
> 安全策略封进 `should_switch`。

---

## 快速开始

```bash
# 1. 取 API Key（在服务器上执行）
cd /path/to/laya-service
export LAYA_API_KEY="$(grep '^LAYA_API_KEY=' .env | cut -d= -f2-)"

# 2. 确认服务活着
curl http://127.0.0.1:9800/healthz

# 3. 调一次推理
curl -X POST http://127.0.0.1:9800/v1/predict \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"state":{"observation":"The x coordinate jumped 2.3 meters."},
       "questions":{"q":{"type":"noul","instructions":"Is the localization reliable?"}}}'
```

---

## 鉴权

所有 `/v1/*` 接口都需要在请求头携带 Bearer Token：

```
Authorization: Bearer <LAYA_API_KEY>
```

**无需鉴权**的路径：`/healthz`、`/readyz`、`/docs`、`/redoc`、`/openapi.json`。

鉴权失败统一返回 **401**，且**不区分**「没带 token」和「token 错误」——避免泄露信息。比对使用常量时间算法，防止时序侧信道。

> **安全提示**：当前服务是明文 HTTP，Key 在网络路径上是可被截获的。公网暴露前建议先上 HTTPS 反代，或改用安全组白名单限制来源 IP。

---

## 统一响应格式

**成功**：

```json
{
  "ok": true,
  "data": { ... },
  "meta": {
    "backend": "auto",
    "model": "convaiinnovations/laya",
    "request_id": "b8434ab7ef0d47cd91b92eaf2a084d6c",
    "latency_ms": 388.28
  }
}
```

**失败**（所有错误路径统一此结构）：

```json
{
  "ok": false,
  "error": {
    "code": "unauthorized",
    "message": "a valid bearer token is required"
  },
  "request_id": "e4b4df6b9ea54428b35c231a1954eb7a"
}
```

`meta` 字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `backend` | string | 计算后端，如 `auto` / `cpu` / `cuda` |
| `model` | string | 实际服务的模型标识 |
| `request_id` | string | 请求关联 ID，排查问题时提供此值 |
| `latency_ms` | float | 服务端处理耗时（毫秒） |

### 请求关联 ID

响应头会返回 `X-Request-ID` 和 `X-Trace-ID`。

- 客户端**可以**自带 `X-Request-ID`（便于跨系统追踪），服务会原样透传并在 `meta.request_id` 中回显
- 不合法（含非法字符）的 ID 会被替换为新生成的，防止 CRLF 头注入
- 报障时请提供 `request_id`，服务端可用它精确定位日志

---

## 错误码表

| `error.code` | HTTP | 含义 | 处理建议 |
|---|---|---|---|
| `unauthorized` | 401 | Token 缺失、格式错误或错误 | 检查 `Authorization` 头 |
| `forbidden` | 403 | 无权限 | 检查凭据 |
| `not_found` | 404 | 路径不存在 | 检查 URL |
| `method_not_allowed` | 405 | HTTP 方法错误 | 用 POST |
| `validation_error` | 422 | 请求体不符合 schema | 看 `message` 里的字段名 |
| `invalid_probability` | 400 | 概率值越界（不在 `[0,1]`） | 检查输入 |
| `invalid_localization_summary` | 400 | 定位摘要内部不自洽 | 检查输入 |
| `invalid_question` | 400 | 问题定义不合法 | 检查 `type` / `instructions` / `criteria` |
| `invalid_decision` | 400 | 决策数据不可用 | 检查输入 |
| `unsupported_schema` | 400 | 工具 schema 无法映射到模型能回答的问题 | 见[兼容层](#兼容层openai--anthropic) |
| `unsupported_model` | 400 | 请求了本服务不提供的模型 | 用 `GET /v1/models` 查可用值 |
| `invalid_api_key` | 400 | API Key 请求字段不合法 | 检查 name / scopes / expires_at |
| `model_load_failed` | 503 | 模型加载失败（缺依赖/下载失败/无磁盘） | **可重试**；查服务端日志 |
| `model_inference_failed` | 503 | 推理过程抛错 | **可重试** |
| `internal_error` | 500 | 未捕获异常 | 提供 `request_id` 报障 |
| `http_error` | 其他 | 框架抛出的其他 HTTP 错误 | 看 HTTP 状态码 |
| `payload_too_large` | 413 | ⚠️ **当前不可达** —— 见下方说明 | 在反向代理层限制 |

**503 是可重试的**，建议客户端对 503 做指数退避重试。500 不建议盲目重试。

> ⚠️ **`MAX_BODY_BYTES` 当前只校验不强制。**
>
> 这个配置项会在启动时校验取值范围，但**没有中间件去实际拦截超大请求体**，Uvicorn
> 也不做限制。所以 `413 payload_too_large` 这条路径**目前走不到** —— 实测发送
> 200 KB 的请求体仍返回 200。
>
> 需要硬性上限，请在**反向代理层**（nginx `client_max_body_size`）实施。详见
> [SECURITY.md](SECURITY.md#known-limitations)。

> 注意：**鉴权在路由之前执行**，所以未鉴权访问一个不存在的路径会返回 401 而非 404。这是刻意的——防止未鉴权调用者通过 404/401 差异探测有哪些路径存在。

---

## 接口详解

### GET /healthz — 存活探针

**是否需要鉴权**：否

判断**进程是否活着**。永远返回 200，不碰模型、不碰网络。失败意味着进程卡死，编排系统应重启它。

**请求**
```bash
curl http://127.0.0.1:9800/healthz
```

**响应 200**
```json
{"status": "ok", "version": "0.1.0"}
```

---

### GET /readyz — 就绪探针

**是否需要鉴权**：否

判断**这个副本能否接流量**。模型未加载完成时返回 **503**。

**请求**
```bash
curl http://127.0.0.1:9800/readyz
```

**响应 200（就绪）**
```json
{
  "status": "ready",
  "model_loaded": true,
  "backend": "auto",
  "model": "convaiinnovations/laya",
  "detail": null
}
```

**响应 503（未就绪）**
```json
{
  "status": "not_ready",
  "model_loaded": false,
  "backend": "auto",
  "model": "convaiinnovations/laya",
  "detail": "the decision model has not been loaded yet; it loads on first request. Send a request to /v1/predict to trigger the load, or set PRELOAD_MODEL=true."
}
```

> ⚠️ **不要把存活探针接到 `/readyz`**。模型冷启动要下载数 GB 权重（数分钟），若用 `/readyz` 做存活判断，容器会在下载中途被反复重启，**永远无法就绪**。存活用 `/healthz`，就绪用 `/readyz`。

---

### POST /v1/predict — 通用推理

对任意 state 回答任意问题。

**是否需要鉴权**：是

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `state` | object | 是 | 情境描述，JSON 可序列化。推荐用 `{"observation": "英文描述"}` |
| `questions` | object | 是 | 问题映射，键为问题 ID，值为问题定义。至少 1 个 |

**问题定义**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | string | 是 | `noul` \| `choice` \| `score` |
| `instructions` | string | 是 | 英文问题描述，非空 |
| `criteria` | object/array | 条件 | `choice` 传映射，`score` 传列表。`noul` 不需要 |

| `type` | 回答形态 | 需要 `criteria` |
|---|---|---|
| `noul` | float — **回答为真的概率** | 否 |
| `choice` | string — 命中的选项键 | 是（映射 `{键: 描述}`） |
| `score` | float — 有序评分的期望值 | 是（列表，从低到高） |

> **只能传英文**。Laya 在英文语料上训练，非英文输入会不可预测地降低准确率。

**请求示例**
```bash
curl -X POST http://127.0.0.1:9800/v1/predict \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "state": {
      "observation": "Over the past 3 seconds, the x coordinate jumped discontinuously by about 2.3 meters, y was stable, heading reversed three times, and positioning confidence dropped from 0.9 to 0.4. The robot is indoors with weak GPS."
    },
    "questions": {
      "location_reliable": {
        "type": "noul",
        "instructions": "Is the current localization reliable?"
      }
    }
  }'
```

**响应 200**
```json
{
  "ok": true,
  "data": {
    "answers": {
      "location_reliable": {
        "type": "noul",
        "noul": 0.2281,
        "confidence": 0.7719,
        "action": {"act_probability": 1.0}
      }
    }
  },
  "meta": {
    "backend": "auto",
    "model": "convaiinnovations/laya",
    "request_id": "6748e31d8abe4100a7fd75007ecba203",
    "latency_ms": 388.28
  }
}
```

**回答字段**（由 Laya 原样返回，服务不做改写）

各类型共有的字段：

| 字段 | 说明 |
|---|---|
| `type` | 问题类型，回显 |
| `confidence` | 模型**对该回答**的把握程度，`[0,1]` |
| `action` | Laya 的附加信号（`act_probability`），一般可忽略 |

各类型特有的字段：

| `type` | 字段 | 类型 | 说明 |
|---|---|---|---|
| `noul` | `noul` | float | 「回答为真」的概率 |
| `choice` | `choice` | string | 命中的选项键 |
| `choice` | `probabilities` | object | 每个选项的概率分布 |
| `score` | `score` | float | 有序评分的期望值 |
| `score` | `legend` | object | 下标 → criteria 描述的映射 |
| `score` | `probabilities` | object | 每个档位的概率分布 |

**`score` / `choice` 的真实响应**（实测）：

```json
{
  "reliable": {"type": "noul", "noul": 0.2369, "confidence": 0.7631,
               "action": {"act_probability": 1.0}},
  "severity": {"type": "score", "score": 1.2513,
               "legend": {"0": "negligible", "1": "minor", "2": "moderate", "3": "severe"},
               "probabilities": {"0": 0.1257, "1": 0.5439, "2": 0.2838, "3": 0.0466},
               "confidence": 0.2121, "action": {"act_probability": 1.0}},
  "cause": {"type": "choice", "choice": "gps",
            "probabilities": {"gps": 0.8965, "slip": 0.05, "imu": 0.0536},
            "confidence": 0.6318, "action": {"act_probability": 1.0}}
}
```

`score` 的 `legend` 是下标到 `criteria` 描述的映射，方便直接读；`score` 值
`1.2513` 表示期望落在 `minor` 和 `moderate` 之间。`probabilities` 给出了完整
分布，比单点值信息量大得多，做下游决策时建议用它而不是只用 `score`。

**多问题示例**
```json
{
  "state": {"observation": "The robot is indoors with weak GPS."},
  "questions": {
    "reliable": {"type": "noul", "instructions": "Is localization reliable?"},
    "severity": {"type": "score", "instructions": "How severe is the drift?",
                 "criteria": ["negligible", "minor", "moderate", "severe"]},
    "cause": {"type": "choice", "instructions": "What is the likely cause?",
              "criteria": {"gps": "GPS multipath", "slip": "Wheel slip", "imu": "IMU drift"}}
  }
}
```

**错误响应**

`422` — 缺字段 / 类型不支持 / `choice`/`score` 缺 `criteria`：
```json
{"ok": false, "error": {"code": "validation_error",
 "message": "questions.location_reliable: question 'location_reliable' requires non-empty 'instructions'"}}
```

---

### POST /v1/robot-dog/localization-reliability — 定位可信度

机器狗专用端点。把「最近定位表现」的结构化摘要，转成「可信 / 不可信」判断 + 建议动作。

**是否需要鉴权**：是

> 与 `/v1/predict` 的区别：这个端点**内置了业务策略**（阈值、回退逻辑、建议动作映射），返回值是**可直接用于控制决策**的。若你只想要模型原始输出，用 `/v1/predict`。

**请求体**

| 字段 | 类型 | 范围 | 必填 | 说明 |
|---|---|---|---|---|
| `x_jump_m` | float | `0 … 1000` | 是 | x 坐标的跳变距离（米） |
| `y_stable` | bool | — | 是 | y 坐标是否稳定 |
| `heading_reversals` | int | `0 … 1000` | 是 | 观测窗口内航向反转次数 |
| `confidence_start` | float | `0 … 1` | 是 | 窗口起点的估计器置信度 |
| `confidence_end` | float | `0 … 1` | 是 | 窗口终点的估计器置信度 |
| `environment` | string | 1–128 字符 | 是 | 环境标签 |
| `confidence_threshold` | float | `0 … 1`，默认 `0.5` | 否 | 判定 `confident` 的门槛 |

**已知 `environment` 取值**：`indoor_weak_gps`、`indoor_strong_gps`、`outdoor_open_sky`、`outdoor_urban_canyon`、`underground`、`indoor`、`outdoor`。
未知标签也接受，会被转成可读英文短语。

**请求示例**
```bash
curl -X POST http://127.0.0.1:9800/v1/robot-dog/localization-reliability \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "x_jump_m": 2.3,
    "y_stable": true,
    "heading_reversals": 3,
    "confidence_start": 0.9,
    "confidence_end": 0.4,
    "environment": "indoor_weak_gps"
  }'
```

**响应 200**
```json
{
  "ok": true,
  "data": {
    "reliable": false,
    "reliability": 0.8382,
    "recommendation": "switch_to_visual_imu",
    "confident": true
  },
  "meta": {
    "backend": "auto",
    "model": "convaiinnovations/laya",
    "request_id": "a5f094cb3c274e88b0b8eaf376a711d1",
    "latency_ms": 726.28
  }
}
```

**响应字段**

| 字段 | 类型 | 说明 |
|---|---|---|
| `reliable` | bool | 定位是否可信。由 `noul` 概率与 **0.5** 比较得出 |
| `reliability` | float | 模型**对该判断**的把握程度，`[0,1]` |
| `recommendation` | string | 建议动作，见下表 |
| `confident` | bool | `reliability` 是否达到 `confidence_threshold` |

**`recommendation` 取值**

| 值 | 含义 |
|---|---|
| `continue_with_current_estimator` | 继续用当前估计器 |
| `switch_to_visual_imu` | 切换到视觉惯性里程计 |

**决策逻辑**（这是业务策略，在应用层，不在路由里）：

1. 若 `confident == false` → `switch_to_visual_imu`
   **模型没把握时，一律建议保守动作。** 机器狗多切一次视觉惯性里程计只是效率损失；相信一个错误的位姿估计可能撞上东西。
2. 否则若 `reliable == false` → `switch_to_visual_imu`
3. 否则 → `continue_with_current_estimator`

> **重要**：`reliable` 和 `reliability` 是**两个不同的量**。前者来自模型的**回答**（`noul` 概率），后者是模型对**该回答的把握**。客户端应当**先看 `confident` 再看 `reliable`**：

```python
if not data["confident"]:
    fallback()                    # 模型没把握 —— 不要采信 reliable
elif data["reliable"]:
    trust_the_estimate()
else:
    switch_to_visual_imu()
```

**错误响应**

`422` — 数值越界：
```json
{"ok": false, "error": {"code": "validation_error",
 "message": "x_jump_m: Input should be greater than or equal to 0"}}
```

---

## 调用示例

### curl（带变量）

```bash
export BASE=http://127.0.0.1:9800
export LAYA_API_KEY="$(grep '^LAYA_API_KEY=' .env | cut -d= -f2-)"

curl -sS -X POST "$BASE/v1/robot-dog/localization-reliability" \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"x_jump_m":2.3,"y_stable":true,"heading_reversals":3,
       "confidence_start":0.9,"confidence_end":0.4,"environment":"indoor_weak_gps"}' \
  | python3 -m json.tool
```

### Python（httpx）

```python
import os
import httpx

BASE = "http://127.0.0.1:9800"
HEADERS = {"Authorization": f"Bearer {os.environ['LAYA_API_KEY']}"}

# timeout 必须给足：冷启动首次请求要下载权重，可能数分钟
with httpx.Client(base_url=BASE, headers=HEADERS, timeout=600.0) as client:
    r = client.post("/v1/robot-dog/localization-reliability", json={
        "x_jump_m": 2.3,
        "y_stable": True,
        "heading_reversals": 3,
        "confidence_start": 0.9,
        "confidence_end": 0.4,
        "environment": "indoor_weak_gps",
    })
    r.raise_for_status()
    body = r.json()

data = body["data"]
request_id = body["meta"]["request_id"]   # 报障时提供

if not data["confident"]:
    fallback()                  # 模型不确定 —— 走保守回退
elif data["reliable"]:
    trust_the_estimate()
else:
    switch_to_visual_imu()
```

### Python（requests）

```python
import os, requests

r = requests.post(
    "http://127.0.0.1:9800/v1/predict",
    headers={"Authorization": f"Bearer {os.environ['LAYA_API_KEY']}"},
    json={
        "state": {"observation": "The x coordinate jumped 2.3 meters."},
        "questions": {"q": {"type": "noul",
                            "instructions": "Is the localization reliable?"}},
    },
    timeout=600,
)
r.raise_for_status()
print(r.json()["data"]["answers"]["q"]["noul"])
```

### 错误处理模板

```python
import httpx, time

def call_with_retry(url, payload, headers, attempts=3):
    """503 可重试；4xx 不要重试。"""
    for i in range(attempts):
        r = httpx.post(url, json=payload, headers=headers, timeout=600)
        if r.status_code == 200:
            return r.json()["data"]
        if r.status_code in (401, 403, 404, 422):
            raise ValueError(f"客户端错误，重试无用: {r.json()['error']['message']}")
        if r.status_code == 503 and i < attempts - 1:
            time.sleep(2 ** i)          # 指数退避
            continue
        raise RuntimeError(f"服务端错误 {r.status_code}: {r.json()}")
    raise RuntimeError("重试耗尽")
```

---

## 关键概念：noul 不是布尔值

这是**最容易用错**的地方。

`noul` 返回的是**「回答为真」的概率浮点数**，不是 `true`/`false`：

```json
{"type": "noul", "noul": 0.2281, "confidence": 0.7719}
```

| 字段 | 含义 |
|---|---|
| `noul` = 0.2281 | 「可靠」这件事为真的概率是 **22.8%** → 即**不可靠** |
| `confidence` = 0.7719 | 模型对「22.8% 这个判断」有 **77% 的把握** |

**模型可以「95% 确信答案是 false」**：`{"noul": 0.05, "confidence": 0.95}`。

两个量必须分开用：

- 判断「是什么」→ 用 `noul`（阈值 0.5）
- 判断「能不能信这个判断」→ 用 `confidence`（对比你的门槛）

> ⚠️ **零样本概率未经校准**。`confidence = 0.77` **不代表**「77% 的情况下是对的」。零样本置信度普遍过度自信，且误差分布不均匀。
>
> **不要**：直接阈值化后当作概率保证；喂进贝叶斯滤波器当似然；向运维展示成「正确率」；单独用于安全攸关的仲裁。
>
> **应当**：先用自有数据微调 + 校准（temperature scaling / isotonic / Platt），再重测可靠性图和 ECE。在那之前，把它当作**排序信号**和**保守回退的触发器**。

---

## 超时与性能

| 场景 | 实测耗时 | 建议客户端 timeout |
|---|---|---|
| 冷启动首次请求（需下载数 GB 权重） | ~90 秒 | **≥ 600 秒** |
| 热态单问题推理 | 300–900 ms | 30 秒足够 |
| 热态机器狗端点（2 个问题，单次前向） | 500–800 ms | 30 秒足够 |

**务必给冷启动留足超时**，否则第一个请求必然超时。

### 其他限制

- **单进程**：模型约占 2 GB RSS。14 GB 机器上**不要**加多 worker，会 OOM。要扩容请横向加机器
- **无请求队列 / 无准入控制**：并发请求会一起加载模型然后争抢 CPU。预期有并发压力时，请在反代层加并发上限
- **CPU 推理慢**：预算是「秒」级，不是「毫秒」级

---

## 兼容层：OpenAI / Anthropic

本服务提供 **OpenAI** 与 **Anthropic** 兼容的接口，让你用现成的官方 SDK 直接连接。
把 SDK 的 `base_url` 指过来即可，不用改客户端代码。

> ### ⚠️ 只支持工具调用，不支持聊天
>
> **本服务背后的模型是分类器，不生成文本。** 它读一段英文，回答结构化问题；
> 它不会写句子——这是模型架构决定的，改不了。
>
> 所以兼容层映射的是两个 API 的**工具调用（tool calling / tool use）**能力：
> 你用工具 schema 描述「要判断什么」，答案以工具调用的参数形式返回。
>
> **聊天请求会被拒绝（400），而不是返回一个看起来像回答的东西。** 这是刻意的：
> 一个假装的生成结果，客户端无法分辨真假，会基于它继续开发。

### 映射规则

工具 schema 的每个属性变成一个模型问题：

| JSON Schema | 模型题型 | 说明 |
|---|---|---|
| `enum`（字符串数组） | `choice` | **顺序即标签顺序**，不会被排序 |
| `oneOf` + `const` | `choice` | 标准写法，可带 `description` 作评分标准 |
| `enum` + `enumDescriptions` | `choice` | OpenAI 的约定，给每个选项配说明 |
| `boolean` | `noul` | ⚠️ **有损**，必须显式给阈值（见下） |
| `number`（有 min/max） | `score` | 返回期望值，按你的范围缩放 |
| `integer`（有 min/max） | `score` | 四舍五入为整数，以符合你的 schema |

**无法映射的形状会被拒绝（400 `unsupported_schema`）**：自由字符串、数组、嵌套对象。
理由同上——猜一个答案比报错危险。

#### 为什么 boolean 必须给阈值

`noul` 返回的是**「为真」的概率**，不是判断。任何替调用方定的阈值都是我们编的，
而且概率会被销毁（调用方无法重新设阈值）。所以：

```jsonc
{
  "type": "boolean",
  "description": "Is this spam?",
  "x-laya-threshold": 0.7      // 必须显式声明
}
```

想拿到概率本身，就用数字类型：

```jsonc
{"type": "number", "minimum": 0, "maximum": 1, "description": "Spam likelihood"}
```

### base_url

| SDK | base_url | 鉴权头 |
|---|---|---|
| OpenAI | `http://<host>:9800/v1` | `Authorization: Bearer <key>` |
| Anthropic | `http://<host>:9800` | `x-api-key: <key>` |

两种鉴权头服务端都接受，所以你也可以用 Bearer 调 Anthropic 接口。

### Anthropic 调用样例

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:9800",      # 注意：不含 /v1
    api_key="laya_sk_...",                 # 或 .env 里的 LAYA_API_KEY
)

message = client.messages.create(
    model="english",
    max_tokens=1024,                       # 必填但被忽略——没有生成可限制
    messages=[{
        "role": "user",
        "content": "Order #12345 arrived broken and I want my money back",
    }],
    tools=[{
        "name": "classify_ticket",
        "description": "Classify a support ticket.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["refund", "technical", "billing"],
                    "description": "Which team should handle this?",
                },
                "churn_risk": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "How likely is the customer to leave?",
                },
                "needs_human": {
                    "type": "boolean",
                    "description": "Does this need a human agent?",
                    "x-laya-threshold": 0.5,
                },
            },
        },
    }],
    tool_choice={"type": "tool", "name": "classify_ticket"},
)

# 答案在 tool_use 块的 input 里（是对象，不是 JSON 字符串）
block = message.content[0]
print(block.type)        # "tool_use"
print(block.name)        # "classify_ticket"
print(block.input)       # {'category': 'refund', 'churn_risk': 0.71, 'needs_human': True}
print(message.stop_reason)   # "tool_use"
```

### OpenAI 调用样例

**`POST /v1/chat/completions`** —— 与 Anthropic 的 `POST /v1/messages` 等价。

```python
import json
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:9800/v1",   # 注意：含 /v1
    api_key="laya_sk_...",
)

response = client.chat.completions.create(
    model="english",
    messages=[{"role": "user", "content": "The app crashes when I upload a photo"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "classify_ticket",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["refund", "technical", "billing"],
                        "description": "Which team should handle this?",
                    },
                },
            },
        },
    }],
    tool_choice={"type": "function", "function": {"name": "classify_ticket"}},
)

call = response.choices[0].message.tool_calls[0]
print(call.function.name)                        # "classify_ticket"
print(json.loads(call.function.arguments))       # {'category': 'technical'}
print(response.choices[0].finish_reason)         # "tool_calls"
```

> 注意两处差异：Anthropic 的 `input` 是**对象**，OpenAI 的 `arguments` 是
> **JSON 字符串**（SDK 会替你解析）。

### curl 样例（Anthropic 格式）

```bash
curl -X POST http://127.0.0.1:9800/v1/messages \
  -H "x-api-key: $LAYA_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "english",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "I was charged twice this month"}],
    "tools": [{
      "name": "classify",
      "input_schema": {
        "type": "object",
        "properties": {
          "category": {
            "type": "string",
            "enum": ["billing", "technical", "refund"],
            "description": "Which team should handle this?"
          }
        }
      }
    }],
    "tool_choice": {"type": "tool", "name": "classify"}
  }'
```

### GET /v1/models

列出可用的 `model` 值：

```bash
curl http://127.0.0.1:9800/v1/models -H "Authorization: Bearer $LAYA_API_KEY"
```

```json
{"object": "list", "data": [{"id": "english", "object": "model",
  "created": 0, "owned_by": "laya-service"}]}
```

### 被忽略的字段

以下字段会被接受但**不起作用**——服务不生成文本，它们没有意义：

`temperature`、`top_p`、`top_k`、`max_tokens`、`stop`、`seed`、`presence_penalty`、
`frequency_penalty`、`logprobs`、`response_format`、`user`、`metadata`、`system`

**会被拒绝的字段**（静默忽略会导致错误解读，所以报错）：

| 字段 | 原因 |
|---|---|
| `stream: true` | 一次前向就出全部答案，没有可流式的内容 |
| `n > 1` | 一次前向只产生一组答案，返回更少会是静默错误 |

### 响应中的 `_laya` 块

两个兼容接口的响应都带一个 `_laya` 元数据块（非标准字段，忽略它不影响使用）：

```json
"_laya": {
  "backend": "auto",
  "latency_ms": 412.3,
  "derived_thresholds": {"needs_human": 0.5},
  "note": "Probabilities from this model are not calibrated. ..."
}
```

`derived_thresholds` 记录了哪些布尔属性是**推导**出来的、用的什么阈值——这样
「模型给的概率」和「我们替你做的判断」是分开的。

---

## API Key 管理

除 `.env` 里的 `LAYA_API_KEY` 外，还可以通过 `/admin/keys` 管理多个 Key。

> **`.env` 里的那个 Key 是管理员 Key**，永远有效，用于创建第一个 Key，也是
> 所有 Key 都被吊销后的恢复通道。它不写入 Key 文件。

所有 `/admin/*` 接口都要求 **admin 权限**——只读推理 Key 无法给自己签发新 Key。

### 存储

Key 存在 JSON 文件里（默认 `data/api_keys.json`，可用 `API_KEYS_PATH` 改）：

- **只存 `sha256` 哈希，不存明文**。明文只在创建响应里出现一次，之后无法找回。
- 文件权限 `0600`。
- 比对使用常量时间算法，且**遍历所有记录不提前退出**——响应时间不泄露 Key 是否存在。

### `GET /admin/keys`

```bash
curl "http://127.0.0.1:9800/admin/keys?include_revoked=false" \
  -H "Authorization: Bearer $LAYA_API_KEY"
```

```json
{
  "keys": [{
    "id": "32e64954c0244a91bb51f642cfef16e4",
    "name": "grafana-prod",
    "prefix": "laya_sk_u825",
    "scopes": ["inference"],
    "created_at": "2026-09-24T03:42:48+00:00",
    "expires_at": null,
    "revoked_at": null,
    "last_used_at": null
  }],
  "available_scopes": ["admin", "inference"],
  "default_scopes": ["inference"]
}
```

### `POST /admin/keys`

```bash
curl -X POST http://127.0.0.1:9800/admin/keys \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "grafana-prod", "scopes": ["inference"]}'
```

```json
{
  "id": "32e64954c0244a91bb51f642cfef16e4",
  "name": "grafana-prod",
  "prefix": "laya_sk_u825",
  "scopes": ["inference"],
  "created_at": "2026-09-24T03:42:48+00:00",
  "secret": "laya_sk_u825..."
}
```

> ⚠️ **`secret` 只在这一条响应里出现。** 存好它，之后无法找回。
> 不传 `scopes` 时默认只有 `inference`；要签发管理员 Key 必须显式写 `["admin", "inference"]`。

### `GET /admin/keys/{key_id}`

```bash
curl http://127.0.0.1:9800/admin/keys/32e64954c0244a91bb51f642cfef16e4 \
  -H "Authorization: Bearer $LAYA_API_KEY"
```

### `PATCH /admin/keys/{key_id}`

改名字、权限或有效期。**未提供的字段保持不变**：

```bash
curl -X PATCH http://127.0.0.1:9800/admin/keys/32e64954c0244a91bb51f642cfef16e4 \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "grafana-staging"}'
```

### `DELETE /admin/keys/{key_id}`

吊销（**立即生效**，记录保留以便审计）：

```bash
curl -X DELETE http://127.0.0.1:9800/admin/keys/32e64954c0244a91bb51f642cfef16e4 \
  -H "Authorization: Bearer $LAYA_API_KEY"
```

---

## 常见问题

**Q：`/readyz` 一直 503？**
模型还没加载完。首次要下载数 GB 权重。看服务端日志里有没有 `laya model loaded`。想启动时就加载，设 `PRELOAD_MODEL=true`。

**Q：第一次请求特别慢，客户端超时了？**
正常。冷缓存需要下载权重。把客户端 timeout 调到 600 秒，或改用 `PRELOAD_MODEL=true` 让启动阶段承担这个成本。

**Q：返回 503 `model_load_failed`？**
模型加载失败。常见原因：磁盘满、HF 端点不可达、`laya` 未安装。响应里只有 `request_id`，具体异常在服务端日志。**可重试**。

**Q：未鉴权访问不存在的路径，为什么是 401 不是 404？**
鉴权在路由之前执行。这是刻意的，防止未鉴权调用者通过 404/401 差异探测路径存在性。

**Q：怎么确认服务在跑？**
```bash
cd /path/to/laya-service && make status   # 显示运行方式、进程、端口、两个探针
```

**Q：公网笔记本连不上？**
1. 先在服务器上确认服务在跑：`make status`
2. 确认阿里云**安全组放行了 9800**（入方向，来源按需限制，不要 `0.0.0.0/0`）
3. 从笔记本测：`curl -m 5 http://<公网IP>:9800/healthz`

**Q：`reliable` 和 `reliability` 到底什么关系？**
`reliable` 是「结论」，`reliability` 是「对结论的把握」。先看 `confident`（`reliability` 是否达标），再看 `reliable`。详见[关键概念](#关键概念noul-不是布尔值)。
