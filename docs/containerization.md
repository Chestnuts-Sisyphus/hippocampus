# 容器化与有状态部署（GF1·③）：Dockerfile、K8s manifests、多副本一致性

> **本机没有 Docker、没有 kubectl、没有 hadolint** —— 所以这份文档里凡是"跑过"的话都要看清：
> `docker build`／`docker run`／`kubectl apply --dry-run=client` **一次都没跑过**。
> 能验的部分已经全部验完，且是**机器验的**，不是"我读过了"：
>
> | 项 | 状态 | 验证方式 |
> |---|---|---|
> | manifests YAML 合法 | ✅ 已验 | `tests/test_n54_r10_gf1_container_static.py`（`yaml.safe_load` 全文件） |
> | kind 齐全（Deployment/Service/ConfigMap/Secret/PVC/HPA/Namespace） | ✅ 已验 | 同上，集合断言 |
> | 密钥不硬编码（Secret 全空值、Deployment 无明文 env） | ✅ 已验 | 同上，正则断言 |
> | 单写者结论（replicas=1／Recreate／RWO／HPA max=1） | ✅ 已验 | 同上，逐条钉 |
> | 探针带令牌（非环回 `/health` 要鉴权） | ✅ 已验 | 同上，命令文本断言 |
> | 镜像构建成功 | ❌ **未验（本机无 Docker）** | —— |
> | 镜像体积 | ❌ **未验（本机无 Docker）** | —— |
> | `/health` 返回 200（容器内） | ❌ **未验（本机无 Docker）** | 但 200 语义在仓内被真机冒烟钉过：`scripts/live_management_smoke.py` |
> | `kubectl apply --dry-run=client` | ❌ **未验（本机无 kubectl）** | 用 YAML 合法性 + 字段断言替代 |
>
> 复跑静态闸：`pytest tests/test_n54_r10_gf1_container_static.py`（18 条，零出站、零 Docker）。

## 一、镜像（`Dockerfile`）

三条口径，逐条对应"面试官会怎么挑刺"：

1. **多阶段**：`builder`（装依赖、建 venv）→ `runtime`（只搬 `/opt/venv` 与源码）。
   运行时镜像里没有编译器、没有 pip 缓存、没有构建上下文。
2. **依赖分层缓存**：先 `COPY pyproject.toml README.md LICENSE` ＋ 一个包骨架 `__init__.py`
   把依赖装进 venv（这一层只在依赖清单变化时失效），再 `COPY src/` 并 `--no-deps` 重装本项目。
   改业务代码只让第二层失效——这是"重建从分钟级到秒级"的那一步。
3. **密钥绝不进镜像**：全文件没有 `COPY` 任何 `.env`／`*.key`／`token`／`config.json`／数据根；
   没有任何 `ENV <KEY>=<值>`。模型凭据只在运行时经环境变量注入（K8s `secretKeyRef`），
   实例令牌只走挂进数据根的 Secret 文件。`.dockerignore` 在构建上下文层再挡一道
   （凭据、`data/`、`.hippocampus/`、`*.db`、`.venv/` 都不进上下文——构建上下文会被送到 daemon，
   历史层是经典泄露面）。

构建与运行（**在有 Docker 的机器上**）：

```bash
docker build -t hippocampus:0.5.0 .
docker run --rm -p 8765:8765 -v hc-data:/data hippocampus:0.5.0
# 首次启动会在 /data 生成 instance_token；之后客户端带 Authorization: Bearer <token>
```

## 二、K8s manifests（`deploy/k8s/`）

| 文件 | 对象 | 要点 |
|---|---|---|
| `00-namespace.yaml` | Namespace | 与同集群其他东西隔离 |
| `10-configmap.yaml` | ConfigMap | **非敏感**项：`HIPPOCAMPUS_HOME=/data`、`HIPPOCAMPUS_OFFLINE=1`、缓存上限、模型名 |
| `20-secret.example.yaml` | Secret | **模板，值为空**；真值由 `kubectl create secret` 或外部密钥管理注入 |
| `30-pvc.yaml` | PVC | `ReadWriteOnce`，5Gi——记忆库/向量索引/令牌同生共死，放同一块卷 |
| `40-deployment.yaml` | Deployment | `replicas: 1` ＋ `Recreate` ＋ 非 root ＋ 只读根 FS ＋ 探针带令牌 |
| `50-service.yaml` | Service | ClusterIP（不对外暴露"能写记忆的管理口"） |
| `60-hpa.yaml` | HPA | `minReplicas: maxReplicas: 1`——**有状态约束下的正确值**，不是没调完 |

部署（**在有 kubectl 的机器上**）：

```bash
kubectl apply --dry-run=client -f deploy/k8s/     # 本机无 kubectl，未跑
kubectl apply -f deploy/k8s/
```

### 2.1 探针为什么必须带令牌（最容易写错的一处）

九轮 W1 的口径是：`hippocampus serve`／`proxy` **默认只绑环回**；要绑非环回必须显式
`--allow-remote`，**且此时 `/health` 与 `/v1/models` 同样要鉴权**（`proxy/app.py:631`
`health_requires_auth=not loopback`）。容器里必然绑 `0.0.0.0`，所以：

- 探针用 `httpGet` 会**一直 401 → Pod 永远不 Ready**（这是真实口径，不是配置笔误）；
- 因此探针写成 `exec` ＋ python 读挂载的 `/data/instance_token` 再带 `Authorization` 发请求
  （不依赖镜像里有 `curl`——slim 镜像没有）；
- `Dockerfile` 的 `HEALTHCHECK` 同口径（带 `Authorization`），否则容器永远 `unhealthy`。

### 2.2 实例令牌为什么走文件不走环境变量

代码只在 `<数据根>/instance_token` 这个位置读令牌（`memory/config.py` `instance_token_path()`
= `runtime.data_root() / "instance_token"`）。所以 Secret 用 **`subPath` 单文件**挂到
`/data/instance_token`，而不是把整个 Secret 卷盖在 `/data` 上（那会把记忆库盖掉）。

## 三、记忆有状态 ⇒ 多副本一致性（书面方案）

**问题的原样**：一个数据根 = 一个 SQLite 主库（`memory.db`）+ 一个 Chroma 持久化目录
+ 一个与卷绑定的实例令牌。这三样都是**单写者**资产：SQLite 靠文件锁，Chroma 的 HNSW 段
依赖同一份本地目录，令牌是"这台实例"的身份。因此**加副本不等于加吞吐，等于制造双写者**。

### 3.1 当前形态（已实现，本仓的答案）

- Deployment `replicas: 1` ＋ `strategy: Recreate` ＋ RWO 卷；
- HPA `maxReplicas: 1`（保留指标观测，把"该扩但被有状态挡住"暴露在 `kubectl describe hpa` 上）；
- 单实例内的并发由仓内既有机制承担：`MemorySession.lock`（RLock，多入口互调不死锁）、
  库内写锁、`HIPPOCAMPUS_SESSION_CACHE_MAX` 让多账户长跑内存有界；
- 可用性靠**重启**（Recreate）而不是靠并行：宁有几秒不可用，不要两个写者。

### 3.2 要真扩容的两条路线（**均未实现，标"设计未实现"**）

**路线 A：共享 RWX 卷 + 选主（读写分离）**

- 卷换 `ReadWriteMany`（NFS/CephFS/云文件存储），Pod 之间靠**租约选主**（K8s Lease 或
  外部协调）选出一个**写者**，其余 Pod 只做**只读检索**；
- 写路径全部收敛到主（写记忆/固化/维护扫描），只读副本读同一份 SQLite（WAL 模式允许多读者）；
- 代价与风险：SQLite 在 NFS 上的锁语义**不可靠**（这是 SQLite 官方明确警告的用法），
  真要这么做，主库应换成服务型数据库（Postgres），SQLite 只留在单机档；
- 还要处理 Chroma 段文件的并发读（读时重建/压缩会让副本读到半成品）。

**路线 B：把状态外置（推荐方向）**

- 记忆主库迁到服务型数据库（Postgres）；向量检索换成**服务型向量库**（Qdrant/Milvus/pgvector）
  或让每个 Pod 自带只读副本 + 变更订阅；
- 此时 Pod 变**无状态**：`replicas` 可 >1，HPA 才有意义，`Recreate` 可换回 `RollingUpdate`；
- 代价：仓内 `MemoryBackend` 协议（`core/backend.py`）已经是这个方向的接口预留
  （SQLite＋Chroma 只是默认实现，`export/import` 已提供目录包迁移含 schema 版本），
  但**真正的远程后端实现没有做**；
- 迁移期一致性：`SUPERSEDES` 边与 `status/lifecycle` 三轴是纯数据，不依赖本地文件系统，
  迁移本身可行；难的是**并发写下的冲突检测**（现在是库内机械规则 + 用户裁决，
  分布式下需要版本号/乐观锁）。

**路线 C（不推荐，但要说清为什么）**：多副本各写各的本地卷 —— 这会直接违反
"记忆是用户的、跨会话一致"这个产品前提，等于把记忆切成互不可见的孤岛。

### 3.3 什么时候该动

判据（可检验）：出现**单实例扛不住的读并发**（只读检索 QPS 打满 CPU）且写入不是瓶颈 →
走路线 A 的只读副本；出现**多实例写入需求**（多区域/高可用写）→ 走路线 B。
在那之前，`maxReplicas: 1` 就是正确答案。
