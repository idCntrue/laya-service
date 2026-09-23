# Laya Service

把 [Laya](https://huggingface.co/convaiinnovations/laya) 非自回归决策引擎封装成
生产级 HTTP 服务的项目，按整洁架构（Clean Architecture）/ 六边形架构
（Hexagonal）组织。

模型对一段情境描述回答自然语言问题，返回布尔判断与概率。本服务把这套能力通过
HTTP 暴露出来，并附带鉴权、结构化日志、健康探针、优雅关闭，以及一套让 Laya
可替换的依赖图。

> **English:** [README.md](README.md)。中英两份文档保持同步；若有出入，以英文版
> 为准（它描述的是代码的实际行为）。

---

## 目录

- [架构](#架构)
- [分层与依赖方向](#分层与依赖方向)
- [目录结构](#目录结构)
- [安装](#安装)
- [运行服务](#运行服务)
- [测试与质量门禁](#测试与质量门禁)
- [API 参考](#api-参考)
- [调用服务](#调用服务)
- [配置](#配置)
- [Docker 部署](#docker-部署)
- [systemd 部署](#systemd-部署)
- [可观测性](#可观测性)
- [故障排查](#故障排查)
- [不可忽视的限制](#不可忽视的限制)
- [安全](#安全)

---

## 架构

```
                          ┌─────────────────────────────────────────┐
   HTTP 请求              │            interfaces（入站适配器）      │
   ──────────────────────▶│  FastAPI 应用、中间件、路由、            │
                          │  请求/响应 schema、异常处理器            │
                          └────────────────────┬────────────────────┘
                                               │ 依赖
                                               ▼
                          ┌─────────────────────────────────────────┐
                          │            application                  │
                          │  用例、DTO、提示词构建器                  │
                          └────────────────────┬────────────────────┘
                                               │ 依赖
                                               ▼
                          ┌─────────────────────────────────────────┐
                          │              domain                     │
                          │  实体、值对象、端口、异常                 │
                          │  ——  零依赖                             │
                          └────────────────────▲────────────────────┘
                                               │ 实现
                          ┌────────────────────┴────────────────────┐
                          │           infrastructure（出站适配器）   │
                          │  LayaDecisionModel 适配器、Settings      │
                          └─────────────────────────────────────────┘
```

从 `infrastructure` 指向 `domain` 的箭头是**实现**箭头，不是**依赖**箭头：
`domain` 声明了 `DecisionModel` 端口，但完全不知道谁去实现它。正是这个反转，让
整个测试套件无需 PyTorch 就能跑起来。

### 请求处理流程

```
POST /v1/robot-dog/localization-reliability
  │
  ├─ RequestIdMiddleware      分配/透传 X-Request-ID
  ├─ AccessLogMiddleware      记录耗时
  ├─ AuthMiddleware           校验 Bearer Token
  ├─ CORS                     （仅在设置了 CORS_ORIGINS 时）
  │
  ├─ routes/robot_dog.py      Pydantic schema → 领域值对象
  │     └─ LocalizationSummary(...)   ← 校验自洽性
  │
  ├─ EvaluateLocalizationUseCase.execute()
  │     ├─ LocalizationPromptBuilder  → 英文 state + questions
  │     └─ DecisionModel.predict()    → 端口
  │            └─ LayaDecisionModel   → importlib → laya → torch
  │
  ├─ routes/robot_dog.py      领域结果 → Pydantic 响应
  └─ exception_handlers.py    领域异常 → HTTP 状态码
```

---

## 分层与依赖方向

| 层 | 可导入 | 禁止导入 |
|---|---|---|
| `domain` | 仅标准库 | 其他一切 —— 无 FastAPI、无 Pydantic、无 Laya |
| `application` | `domain`、标准库 | `infrastructure`、`interfaces`、任何框架 |
| `infrastructure` | `domain`、`application`、框架 | `interfaces` |
| `interfaces` | 所有内层 | — |

**这不是约定，是被测试强制的。** `tests/unit/test_architecture.py` 用 `ast`
解析每个模块的导入，一旦违反分层就让构建失败。如果你在 domain 文件里写了
`import fastapi`，`make test` 立刻变红。

**各层职责：**

- **业务规则**（什么叫「不可靠」、该给出什么建议、模型不确定时怎么办）→
  `application/use_cases/`
- **不变量**（概率必须在 `[0,1]`；跳变距离非负）→ `domain/value_objects/`
- **Laya、torch、HTTP、环境变量** → `infrastructure/` 和 `interfaces/`

---

## 目录结构

```
.
├── .env.example              配置模板 —— 复制为 .env
├── .editorconfig             编辑器一致性
├── .pre-commit-config.yaml   ruff、ruff-format、mypy、空白符钩子
├── Makefile                  所有开发与运维入口
├── pyproject.toml            打包、ruff、mypy、pytest、覆盖率配置
├── README.md                 英文主文档
├── README.zh-CN.md           本文件
├── API.md                    中文接口文档
├── API.en.md                 英文接口文档
├── CHANGELOG.md              变更记录
├── CONTRIBUTING.md           贡献指南
├── SECURITY.md               安全策略与威胁模型
├── LICENSE                   Apache-2.0
├── requirements/
│   ├── base.txt              运行时依赖
│   ├── dev.txt               base + 测试/检查工具
│   └── prod.txt              base + gunicorn
├── src/laya_service/
│   ├── __init__.py           __version__
│   ├── main.py               入口 → uvicorn.run(factory=True)
│   ├── logging_config.py     JSON 格式化器、StructuredLogger、密钥脱敏
│   ├── domain/               ← 零依赖
│   │   ├── entities/decision.py          Decision、Prediction
│   │   ├── value_objects/
│   │   │   ├── probability.py            Probability（校验 [0,1]）
│   │   │   └── localization_summary.py   LocalizationSummary
│   │   ├── ports/decision_model.py       DecisionModel 协议
│   │   └── exceptions.py                 DomainError 异常体系
│   ├── application/          ← 仅依赖 domain
│   │   ├── dto/                          PredictInput/Output、LocalizationInput/Output
│   │   ├── use_cases/                    PredictDecision、EvaluateLocalization
│   │   └── services/                     LocalizationPromptBuilder
│   ├── infrastructure/       ← 适配器
│   │   ├── config/settings.py            pydantic-settings + lru_cache 单例
│   │   └── model/laya_adapter.py         唯一允许 import laya 的模块
│   └── interfaces/http/      ← 入站适配器
│       ├── app.py                        create_app() 工厂
│       ├── dependencies.py               依赖注入组装根
│       ├── exception_handlers.py         异常 → 状态码映射表
│       ├── middleware/                   auth、request_id、access_log
│       ├── routes/                       health、predict、robot_dog
│       └── schemas/                      Pydantic 请求/响应模型
├── tests/
│   ├── conftest.py           FakeDecisionModel、TestClient fixture
│   ├── unit/                 domain、application、infrastructure、architecture
│   └── integration/test_api.py
├── scripts/                  start/stop/status/logs/smoke_test/install_systemd
├── deploy/
│   ├── docker/Dockerfile     多阶段、非 root
│   └── systemd/              laya-service.service.in（模板）
└── .github/
    ├── workflows/ci.yml      lint + typecheck + test + docker
    ├── ISSUE_TEMPLATE/
    ├── PULL_REQUEST_TEMPLATE.md
    └── dependabot.yml
```

---

## 安装

### 前置条件

- Python **3.10+**（已在 3.10.12 验证）
- 虚拟环境约占 3 GB 磁盘，首次推理还需下载模型权重
- **不需要 GPU**。`laya-mlx` 是 **Apple Silicon 专用** —— 不要在 x86_64 Linux
  上安装它

### 一条命令

```bash
make install-dev
```

这会创建 `.venv`、安装 CPU 版 PyTorch，然后安装项目和开发依赖。

### 为什么必须装 CPU 版 PyTorch

`laya` 依赖 `torch>=2.0.0`。在没有 NVIDIA GPU 的机器上，直接
`pip install torch` 仍会下载 **CUDA 版（约 2.5 GB）**以及一堆永远加载不了的
`nvidia-*` wheel。`make install-torch` 会先装 `torch==2.9.1+cpu`（约 176 MB），
让依赖解析器根本不会考虑 CUDA 版本。

手动安装请遵循同样顺序：

```bash
python3 -m venv .venv
.venv/bin/pip install torch==2.9.1+cpu --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -e ".[dev]"
```

### 如果 `python3 -m venv` 报 "ensurepip is not available"

部分精简版 Debian/Ubuntu 镜像的 `python3` 不带 `python3-venv`。Makefile 已处理
这种情况：`make venv` 用 `--without-pip` 创建环境，再用 `get-pip.py` 引导安装
pip。**全程无需 sudo。**

---

## 运行服务

```bash
# 生成密钥（绑定公网接口前必须）
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# 写入 .env 的 LAYA_API_KEY=...

make start      # 后台启动，PID 写入 .run/laya.pid，日志写入 logs/laya.log
make status     # 运行方式、进程、端口、/healthz、/readyz
make logs       # tail JSON 日志
make stop       # SIGTERM，30 秒后升级为 SIGKILL
make restart
```

开发时前台运行并自动重载：

```bash
make run
```

### 第一次请求会很慢

模型是**懒加载**的。第一次调用 `/v1/predict` 会导入 PyTorch 并下载权重 ——
冷缓存下需要**数分钟**，占用数 GB 磁盘。

```
/readyz  →  503 not_ready   （首次请求之前）
/readyz  →  200 ready       （模型预热之后）
```

如果希望把这个成本放在启动阶段而不是首次请求：

```bash
PRELOAD_MODEL=true
```

**绝对不要把存活探针接到 `/readyz`。** 否则容器会在下载中途被反复重启，永远
无法就绪。存活探针用 `/healthz`，`/readyz` 用于负载均衡成员判断。

---

## 测试与质量门禁

```bash
make lint          # ruff check
make format        # ruff check --fix && ruff format
make typecheck     # mypy --strict
make test          # pytest
make test-cov      # pytest --cov，低于 70% 直接失败
make check         # lint + format-check + typecheck + test
make smoke         # 对运行中的服务跑三条 curl
```

`mypy --strict` 和 70% 覆盖率下限都是强制的 —— `make test-cov` 低于阈值会返回
非零退出码。

测试套件跑得很快，因为 `FakeDecisionModel` 实现了 `DecisionModel` 端口。无需
权重、无需网络、无需 GPU。

> **改了 `pyproject.toml`？请重跑一次安装。** `make test` **不验证项目能否被
> 安装** —— 它从已经构建好的 editable 检出里导入。元数据错误在重新构建之前
> 完全不可见：
>
> ```bash
> .venv/bin/pip install -e ".[dev]"
> ```
>
> 这不是假设：PEP 639 的 license 表达式配上残留的 `License ::` classifier，
> 本地 308 个测试全绿，而每个 CI job 全挂。详见
> [CONTRIBUTING.md](CONTRIBUTING.md#quality-gates)。

---

## API 参考

完整接口文档见 **[API.md](API.md)**（中文）和 **[API.en.md](API.en.md)**（英文）。

概览：

| 端点 | 鉴权 | 用途 |
|---|---|---|
| `GET /healthz` | 否 | 存活探针，永远 200，不碰模型 |
| `GET /readyz` | 否 | 就绪探针，模型未加载时 503 |
| `POST /v1/predict` | 是 | 通用推理，支持 `noul` / `choice` / `score` |
| `POST /v1/robot-dog/localization-reliability` | 是 | 定位可信度判断 + 建议动作 |

交互式文档：`http://<host>:9800/docs`

---

## 调用服务

### curl

```bash
source .env

curl -sS -X POST http://127.0.0.1:9800/v1/robot-dog/localization-reliability \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"x_jump_m":2.3,"y_stable":true,"heading_reversals":3,
       "confidence_start":0.9,"confidence_end":0.4,
       "environment":"indoor_weak_gps"}' | python3 -m json.tool
```

### Python

```python
import os
import httpx

BASE = "http://127.0.0.1:9800"
HEADERS = {"Authorization": f"Bearer {os.environ['LAYA_API_KEY']}"}

# timeout 必须给足：冷启动要下载权重，可能数分钟
with httpx.Client(base_url=BASE, headers=HEADERS, timeout=600.0) as client:
    response = client.post(
        "/v1/robot-dog/localization-reliability",
        json={
            "x_jump_m": 2.3,
            "y_stable": True,
            "heading_reversals": 3,
            "confidence_start": 0.9,
            "confidence_end": 0.4,
            "environment": "indoor_weak_gps",
        },
    )
    response.raise_for_status()
    data = response.json()["data"]

if not data["confident"]:
    # 模型不确定 —— 不要采信 reliable，走保守回退
    fallback()
elif data["reliable"]:
    trust_the_estimate()
else:
    switch_to_visual_imu()
```

注意 `timeout=600` —— 第一次调用要下载模型权重。

### 服务器上其他项目

本机已装好 Claude Code skill `laya-call`，可以直接复用现成客户端，不必重新
实现：

```python
import sys; sys.path.insert(0, "<skill目录>/laya-call/scripts")
from laya_client import LayaClient

client = LayaClient.from_env()          # 自动读取端口和 API Key
r = client.localization_reliability(
    x_jump_m=2.3, y_stable=True, heading_reversals=3,
    confidence_start=0.9, confidence_end=0.4, environment="indoor_weak_gps")
if r.should_switch: switch_to_visual_imu()
```

客户端自带 503 重试、600 秒冷启动超时，并把「不自信就保守回退」的安全策略封进
`should_switch`。

---

## 配置

所有配置都由 `pydantic_settings` 从环境变量和 `.env` 读取，**没有任何硬编码**。
把 `.env.example` 复制为 `.env` 即可。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LAYA_BACKEND` | `auto` | `auto` \| `cpu` \| `cuda` \| `mlx` |
| `LAYA_MODEL` | *(空)* | 模型 id；留空用 Laya 默认模型 |
| `HF_ENDPOINT` | `https://hf-mirror.com` | 权重下载端点 |
| `HOST` | `0.0.0.0` | 绑定网卡 |
| `PORT` | `9800` | 绑定端口 |
| `LAYA_API_KEY` | *(空)* | Bearer Token。`HOST` 非 loopback 时**必填** |
| `LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL` |
| `CORS_ORIGINS` | *(空)* | 逗号分隔白名单；留空即完全关闭 CORS |
| `MAX_BODY_BYTES` | `65536` | 请求体上限（硬顶 10 MiB） |
| `PRELOAD_MODEL` | `false` | 启动时加载权重 |
| `MODEL_CACHE_DIR` | *(空)* | 覆盖权重缓存目录 |
| `REQUEST_TIMEOUT_S` | `120` | 单次推理预算 |
| `HF_HUB_DISABLE_XET` | 本部署为 `1` | 关闭 Xet CAS 传输协议（见故障排查） |

`SYSTEMD_NO_NEW_PRIVILEGES`、`SYSTEMD_PRIVATE_DEVICES`、
`SYSTEMD_PROTECT_KERNEL_MODULES` 由 **systemd unit** 读取，不由应用读取。见
[systemd 部署](#systemd-部署)。

### API Key 规则

如果 `HOST` **不是** `127.0.0.1` / `localhost` / `::1`，且 `LAYA_API_KEY` 为空，
服务会**拒绝启动**：

```
configuration error: HOST='0.0.0.0' is not a loopback address but LAYA_API_KEY is empty.
Set LAYA_API_KEY, or bind HOST=127.0.0.1 to run without authentication.
```

这是刻意的。一个无鉴权、绑定在可路由接口上的推理端点，等于向任何发现它的人
开放你的 CPU。

---

## Docker 部署

```bash
make docker-build     # 多阶段构建、非 root、CPU 版 torch
make docker-run       # 读取 .env，映射 PORT
make docker-stop
```

镜像以 uid 10001（非 root）运行，设置 `PYTHONUNBUFFERED=1` 和
`PYTHONDONTWRITEBYTECODE=1`，暴露 8000（**容器内**端口，宿主机映射任意），并用
`HEALTHCHECK` 探测 `/healthz`（存活，不是就绪 —— 原因见上文）。

**务必挂载模型缓存卷**，否则每次容器重启都要重新下载数 GB：

```bash
docker run -d --name laya-service \
  --env-file .env \
  -p 9800:8000 \
  -v laya-hf-cache:/data/hf \
  --restart unless-stopped \
  laya-service:latest
```

CUDA 版可在构建时覆盖 torch 索引：

```bash
docker build -f deploy/docker/Dockerfile \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121 \
  -t laya-service:cuda .
```

### 镜像的两个刻意设计

- **torch 安装必须带 `--extra-index-url`，不能只用 `--index-url`。** torch 索引
  只有 wheel、没有源码包，所以只用 `--index-url` 时 pip 取不到某些 wheel 声明的
  构建后端（`flit_core`），构建会失败并报
  `Could not find a version that satisfies the requirement flit_core`。保留 PyPI
  作为兜底索引；显式的 `==2.9.1+cpu` 固定版本仍能阻止它提供 CUDA 版 torch。
- **运行时阶段只装 `libgomp1`，`HEALTHCHECK` 用 `urllib` 而不是 `curl`。**
  `libgomp1` 是必需的 —— torch 的 `libtorch_cpu.so` 链接了 `libgomp.so.1`，缺了
  它容器能启动但会在 `import torch` 时崩溃。`curl` 则非必需，去掉它同时避免拉取
  Debian apt 索引 —— 在慢镜像源上，那是整个构建最慢的一步。

  如果你新增了需要源码编译的依赖，请在 **builder** 阶段加 `build-essential`
  （该阶段最终会被丢弃）。

---

## systemd 部署

unit 以**模板**形式提供：`deploy/systemd/laya-service.service.in`。

**不要直接安装模板** —— 里面有 `@PLACEHOLDER@` 占位符。用生成脚本：

```bash
./scripts/install_systemd.sh              # 安装并 reload
./scripts/install_systemd.sh --dry-run    # 只打印，不改动任何东西
./scripts/install_systemd.sh --enable     # 安装 + 启用 + 启动

systemctl --user status laya-service
journalctl --user -u laya-service -f
systemctl --user restart laya-service
```

想注销后仍保持运行：

```bash
sudo loginctl enable-linger "$USER"
```

### 为什么要用模板

systemd 需要绝对路径，而含空格的路径在不同指令下处理方式不同。以下规则是在
Ubuntu 22.04 上**实测**得出的，不是猜测：

| 指令 | 空格处理 |
|---|---|
| `ExecStart` | **支持引号** —— 用双引号包起来 |
| `WorkingDirectory` / `EnvironmentFile` | **不支持引号** —— 整行剩余部分都是值，空格直接用；加引号反而会把引号写进路径 |
| `ReadWritePaths` | **支持引号** —— 双引号是正确写法 |

两种看似合理但**会失败**的写法：

- **裸空格** → 被拆成两个非法路径，systemd 各自静默丢弃并打警告
- **`\x20` 或 `\040`** → systemd 去掉反斜杠保留字面字符，路径里出现 `x20`，
  然后匹配不到任何东西

生成脚本会自动处理这三者。

### 如果 unit 启动失败并报 `status=218/CAPABILITIES`

在容器或受限虚拟机里的**用户级** systemd manager 上，有三条加固指令无法生效，
因为 manager 拿不到它们需要的 capability：

| 指令 | 需要 | 症状 |
|---|---|---|
| `NoNewPrivileges=true` | `CAP_SETPCAP` | `Failed at step CAPABILITIES ... Operation not permitted` |
| `PrivateDevices=true` | `CAP_SYS_ADMIN` | 同上 |
| `ProtectKernelModules=true` | `CAP_SYS_ADMIN` | 同上 |

**三者各自独立失败**，所以只去掉一个不够。这三条已在 unit 中参数化，从 `.env`
关闭即可：

```bash
SYSTEMD_NO_NEW_PRIVILEGES=false
SYSTEMD_PRIVATE_DEVICES=false
SYSTEMD_PROTECT_KERNEL_MODULES=false
```

然后 `systemctl --user daemon-reload && systemctl --user restart laya-service`。

确认实际生效值 —— **不要假设**：

```bash
systemctl --user show laya-service \
  -p NoNewPrivileges -p PrivateDevices -p ProtectKernelModules
```

正常主机（非容器）请保持三者开启。

---

## 可观测性

### 结构化 JSON 日志

每行是一个 JSON 对象：

```json
{"timestamp":"2026-09-22T10:14:03+0000","level":"INFO","logger":"laya_service.interfaces.http.middleware.access_log","message":"request completed","method":"POST","path":"/v1/predict","status":200,"latency_ms":1843.21,"request_id":"9f2c1e4b...","trace_id":"9f2c1e4b...","client":"127.0.0.1"}
```

关键字段：`request_id`、`trace_id`、`latency_ms`、`method`、`path`、`status`、
`client`。

耗时超过 1 秒的请求记为 `WARNING`，无需额外的延迟看板即可发现。

**绝不记录：** 请求体、响应体、`Authorization` 头。`SecretRedactingFilter` 会
兜底擦除 Bearer Token 和 `key=value` 形式的凭据。

### 关联追踪

调用方若自带合法的 `X-Request-ID`（匹配 `[A-Za-z0-9._:-]{1,128}`）会被沿用，
否则自动生成。它会在响应头返回，并在 `meta.request_id` 中回显。该净化逻辑同时
阻断 CRLF 头注入和日志伪造。

### 接入 ELK / Loki

输出是换行分隔的 JSON，没有多行记录（堆栈被转义进单个 `exception` 字符串字段），
可直接消费，无需自定义解析器：

```yaml
# Promtail 示例
scrape_configs:
  - job_name: laya-service
    static_configs:
      - targets: [localhost]
        labels:
          job: laya-service
          __path__: /path/to/laya-service/logs/laya.log
    pipeline_stages:
      - json:
          expressions:
            level: level
            request_id: request_id
            latency_ms: latency_ms
```

---

## 故障排查

### `configuration error: HOST=... but LAYA_API_KEY is empty`

按设计工作。在 `.env` 里设置 `LAYA_API_KEY`，或绑定 `HOST=127.0.0.1`。

### `/readyz` 一直返回 503

模型没加载完。按可能性排序：

1. **权重还在下载。** 看日志 —— 适配器在下载前打 `loading laya model`，完成后
   打 `laya model loaded`。首次加载可能数分钟。
2. **磁盘满。** 权重需要数 GB。`df -h`。
3. **`HF_ENDPOINT` 不可达。** 用 `curl -I https://hf-mirror.com` 测。可试
   `https://huggingface.co`。
4. **`laya` 导入失败。** 检查 `.venv/bin/python -c "import laya"`。

加载失败会被缓存，所以坏掉的安装会在后续每次请求上快速失败，而不是反复重试一次
数分钟的下载。修复原因后重启。

### `/v1/predict` 返回 503 `model_load_failed`

同上。日志里有底层异常；客户端只拿到关联 ID。

### `CAS Client Error: HTTP status client error (401 Unauthorized), domain: https://cas-server.xethub.hf.co/...`

这个错误很具体，值得认识。它意味着 **Xet** 传输协议（`huggingface_hub` 通过
`hf_xet` 对大文件使用）被一个不支持它的镜像代理了。该镜像能正常提供小文件和
元数据 API（返回 200），所以很让人困惑：**只有 CAS blob 路径失败**。

修复：关闭 Xet，让 `huggingface_hub` 回退到普通 HTTP 范围请求。

```bash
echo 'HF_HUB_DISABLE_XET=1' >> .env
make restart
```

本部署的 `.env` 默认已设置此项。如果你把 `HF_ENDPOINT` 指向源站
（`https://huggingface.co`）且网络可达，可以删掉它，恢复 Xet 更快的传输。

### `huggingface.co` 不可达 / 请求卡住

有些网络完全连不上源站。默认 `HF_ENDPOINT=https://hf-mirror.com` 正是为此。注意
镜像可能滞后于上游的**新发布**，且（如上）不代理 Xet。

### 即使预热了，请求仍要 60–90 秒

在 CPU 上，Laya 对少量问题做推理需要数十秒 —— 预热后响应里的 `latency_ms` 反映
了这一点（本机单问题预热后约 300–900 ms；首次调用因权重要换入会明显更久）。请
据此设置预算，并优先使用 `/v1/robot-dog/...` 端点 —— 它一次前向回答两个问题，
而不是两次往返。

### 笔记本从公网连不上

1. 先在服务器上确认服务在跑：`make status`
2. 确认阿里云**安全组放行了对应端口**（入方向，来源按需限制，**不要**
   `0.0.0.0/0`）
3. 从笔记本测：`curl -m 5 http://<公网IP>:9800/healthz`

---

## 不可忽视的限制

**Laya 输出的是零样本概率，它们没有经过校准。**

这是本文档中最重要的一条。`/v1/robot-dog/localization-reliability` 返回的
`reliability` 是*模型自报的把握程度*，**不是频率**。`0.9` **不代表**这个判断有
90% 的概率是对的。零样本置信度通常**过度自信**，而且这种失准在不同输入上并不
均匀。

实测佐证：本服务上答错的几个问题，模型给出的把握分别是 0.81、0.85、0.92 ——
**它会自信地答错**。所以你不能靠给 `confidence` 设阈值来过滤错误。

具体来说，**不要**：

- 给 `reliability` 设个调好的阈值，然后把结果当作概率保证
- 未经校准就把它作为似然喂进下游贝叶斯滤波器
- 把它当作「正确率」展示给运维人员
- 单独用它做安全攸关的仲裁

在以上任何一种用法之前，请先**在自己的标注轨迹上微调并校准**（temperature
scaling、isotonic 回归或 Platt scaling），再用留出集重新测可靠性图和 ECE。

在那之前，把它当作**排序信号**和**保守回退的触发器**。服务自身的策略也遵循这
一点：模型不自信时，无论判断如何都建议 `switch_to_visual_imu`。

### 模型擅长什么、不擅长什么

在本服务上实测：

| 任务类型 | 结果 |
|---|---|
| 语义分类（情感、垃圾邮件、意图、内容安全） | 8/8 正确 |
| 推理（算术、时间、多步） | 3/6 正确 |

**它读文本，不做计算。** 算术、空间推理、规划、搜索这类任务，确定性算法在准确率
和成本上都会胜过它。

### 其他限制

- **冷启动很慢。** 冷缓存下需数分钟；请在探针和超时上留出余量。
- **默认单进程。** 模型约占数 GB RSS。14 GB 主机上，多于一个 Uvicorn worker
  很可能 OOM。扩容前先测量；应当**横向加机器**，而不是加 worker。
- **CPU 推理慢。** 预算是「秒」级，不是「毫秒」级。
- **没有请求队列和准入控制。** 并发请求会一起加载模型，然后争抢 CPU。预期有
  负载时，请在服务前面加并发上限。
- **`MAX_BODY_BYTES` 只做校验，未强制。** Uvicorn 也不强制。需要硬上限请在反向
  代理层实施。

---

## 安全

### API Key

- 用 `secrets.token_urlsafe(32)` 生成 —— 绝不用人选的字符串
- 比对使用 `hmac.compare_digest`（常量时间），响应延迟不会逐字节泄露密钥
- 鉴权失败信息统一：客户端只知道「失败了」，别无所知
- 密钥在启动日志中被脱敏，且被日志过滤器兜底擦除
- **轮换方式**：改 `.env` 后 `make restart`。没有双密钥重叠期，所以轮换会有短暂
  中断 —— 请在维护窗口进行，或在前置代理上做零停机轮换

### 网络暴露

- **绝不要在无密钥的情况下绑定 `0.0.0.0`。** 配置校验器会阻止，但你要放在前面
  的东西同样需要小心
- 安全组/防火墙应限制到确实需要访问的来源网段。**不要**对 `0.0.0.0/0` 开放
- 建议用反向代理（nginx、Caddy、ALB）终止 TLS。**本服务是明文 HTTP。** 明文传输
  的 Bearer Token，路径上任何人都能读到
- 若对外公开，请在代理层加限流。服务自身没有限流，而每个请求都在消耗真实 CPU

### CORS

`CORS_ORIGINS` 默认为空，即**完全关闭 CORS** —— 这对服务端到服务端的 API 是正确
的。只有浏览器需要直接调用时才设置，并列出精确的来源。**绝不要用 `*`** —— 配合
Bearer Token，它等于邀请凭据通过被攻陷的页面泄露。

### 请求体大小

`MAX_BODY_BYTES` 默认 64 KiB。注意[限制](#不可忽视的限制)里提到的：这目前是校验
配置而非强制限制。重要的话请在代理层实施。

### 容器与 unit 加固

Docker 镜像以非 root 用户运行，对应用代码无写权限。systemd unit 设置了
`NoNewPrivileges`、`PrivateTmp`、`PrivateDevices`、`ProtectSystem=strict`、
`ProtectHome=read-only` 等，并只重新开放服务确实需要写入的三个路径。

---

## 许可证

Apache License 2.0，详见 [LICENSE](LICENSE)。
