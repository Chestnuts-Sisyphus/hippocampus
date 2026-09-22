"""GF1·③ 静态闸：容器化产物逐条核对（本机无 Docker/kubectl，能验的都验在闸里）。

为什么要有这个文件：本机**没有 Docker 也没有 kubectl**，所以 `docker build` 与
`kubectl apply --dry-run=client` 都跑不了——那就不能靠"我读过了"当验证。把
"能被机器检查的部分"全部写成断言：

- manifests 合法性：每个 YAML 都能 `yaml.safe_load`，kind 齐全（Deployment/Service/
  ConfigMap/Secret/PVC/HPA），namespace 一致，Deployment 的 selector 与 pod 标签对得上；
- **密钥不进镜像**：Secret 清单里每个值都是空串；Deployment 里没有任何明文密钥 env；
  Dockerfile 里没有任何能带进密钥的 `COPY`／`ENV`；
- **有状态结论**：`replicas: 1` + `strategy: Recreate` + RWO 卷 + HPA `maxReplicas: 1`
  —— 这四条是一组，改任何一条都会让"单写者"前提失效，所以一起钉；
- 探针口径：绑非环回时 `/health` 也要令牌，**探针命令必须带 Authorization**，
  否则 Pod 永远不 Ready（这是九轮 W1 的真实口径，不是过度设计）。

跑法：`pytest tests/test_n54_r10_gf1_container_static.py`（纯静态，零出站，零 Docker）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
K8S_DIR = ROOT / "deploy" / "k8s"
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"

REQUIRED_KINDS = {"Namespace", "ConfigMap", "Secret", "PersistentVolumeClaim", "Deployment", "Service", "HorizontalPodAutoscaler"}


def _manifests() -> list[tuple[Path, dict]]:
    files = sorted(K8S_DIR.glob("*.yaml"))
    return [(f, yaml.safe_load(f.read_text(encoding="utf-8"))) for f in files]


def _by_kind(kind: str) -> dict:
    for _, doc in _manifests():
        if doc and doc.get("kind") == kind:
            return doc
    raise AssertionError(f"manifests 里缺 kind={kind}")


def test_every_manifest_is_valid_yaml_and_kind_set_is_complete():
    """① 全部 manifests 合法且 kind 齐全（等价于 `kubectl apply --dry-run=client` 能过的语法底线）。"""
    manifests = _manifests()
    assert len(manifests) >= len(REQUIRED_KINDS), f"manifest 文件太少：{[str(p.name) for p, _ in manifests]}"
    kinds = {doc["kind"] for _, doc in manifests}
    assert REQUIRED_KINDS <= kinds, f"缺 kind：{REQUIRED_KINDS - kinds}"


def test_namespace_is_uniform():
    """② 除 Namespace 自身外，所有对象都落在 hippocampus 命名空间。"""
    for path, doc in _manifests():
        if doc["kind"] == "Namespace":
            continue
        assert doc["metadata"].get("namespace") == "hippocampus", f"{path.name} 命名空间不一致"


def test_secret_manifest_carries_no_real_values():
    """③ Secret 模板里**没有真值**：每个 stringData 值都是空串。

    这条就是"密钥只走 env/Secrets、绝不进镜像也绝不进 git"在清单侧的机器判据——
    一旦有人把真 key 填进来提交，这里立刻红。
    """
    secret = _by_kind("Secret")
    values = secret.get("stringData") or {}
    assert values, "Secret 应至少声明出需要注入的 key 名"
    non_empty = {k: v for k, v in values.items() if str(v).strip()}
    assert not non_empty, f"Secret 模板里出现了非空值，禁止提交真凭据：{list(non_empty)}"


def test_deployment_env_has_no_literal_secret():
    """④ Deployment 的 env：凭据类变量必须来自 secretKeyRef，不能写死值。"""
    containers = _by_kind("Deployment")["spec"]["template"]["spec"]["containers"]
    for container in containers:
        for entry in container.get("env") or []:
            name = entry["name"]
            if re.search(r"KEY|TOKEN|SECRET|PASSWORD", name, re.I):
                assert "valueFrom" in entry, f"{name} 用了明文 value，必须走 secretKeyRef"
                assert "value" not in entry, f"{name} 同时给了明文 value"
        # envFrom 只允许 ConfigMap（非敏感）；Secret 必须逐个 key 显式引用
        for ref in container.get("envFrom") or []:
            assert "secretRef" not in ref, "envFrom 批量注入 Secret 会绕过逐项审计，改为 secretKeyRef"


def test_stateful_conclusions_are_pinned():
    """⑤ 单写者四条一起钉：replicas=1／Recreate／RWO／HPA max=1。

    记忆是 SQLite+C 目录的有状态体：多副本＝双写者。这四条是本项目**有意**的选择，
    不是没调完的默认值（扩容路线见 docs/containerization.md §三，标"设计未实现"）。
    """
    assert _by_kind("Deployment")["spec"]["replicas"] == 1
    assert _by_kind("Deployment")["spec"]["strategy"]["type"] == "Recreate"
    assert _by_kind("PersistentVolumeClaim")["spec"]["accessModes"] == ["ReadWriteOnce"]
    hpa = _by_kind("HorizontalPodAutoscaler")["spec"]
    assert hpa["minReplicas"] == 1 and hpa["maxReplicas"] == 1, "有状态单写者前提下不许抬高副本数"


def test_deployment_selector_matches_pod_labels_and_hpa_targets_it():
    """⑥ 接线自洽：selector ↔ pod 标签 ↔ HPA 的 scaleTargetRef 指向同一个 Deployment。"""
    deployment = _by_kind("Deployment")
    assert deployment["spec"]["selector"]["matchLabels"] == deployment["spec"]["template"]["metadata"]["labels"]
    name = deployment["metadata"]["name"]
    target = _by_kind("HorizontalPodAutoscaler")["spec"]["scaleTargetRef"]
    assert (target["kind"], target["name"]) == ("Deployment", name)
    assert _by_kind("Service")["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]


def test_non_loopback_bind_requires_the_allow_remote_flag():
    """⑦ 容器绑 0.0.0.0 必须带 `--allow-remote`，否则 CLI 退出码 2 拒起（九轮 W1 启动闸）。"""
    args = _by_kind("Deployment")["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--allow-remote" in args, "缺 --allow-remote 会让容器起不来（启动闸拒起）"
    assert "0.0.0.0" in args, "容器内必须绑 0.0.0.0，否则 Service 打不通"


def test_probes_carry_authorization_because_health_needs_a_token():
    """⑧ 探针必须带令牌：绑非环回时 `/health` 也要鉴权（app.py: health_requires_auth=not loopback）。"""
    container = _by_kind("Deployment")["spec"]["template"]["spec"]["containers"][0]
    for probe_name in ("readinessProbe", "livenessProbe"):
        probe = container[probe_name]
        assert "exec" in probe, f"{probe_name} 用 httpGet 无法带 Secret 里的令牌，必须用 exec"
        script = "\n".join(probe["exec"]["command"])
        assert "instance_token" in script, f"{probe_name} 没读实例令牌文件"
        assert "Authorization" in script, f"{probe_name} 没带 Authorization → 非环回下会一直 401"


def test_token_is_mounted_as_single_file_into_data_root():
    """⑨ 令牌以 subPath 单文件挂进数据根：代码只在 <数据根>/instance_token 读它。"""
    container = _by_kind("Deployment")["spec"]["template"]["spec"]["containers"][0]
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    token_mount = mounts["instance-token"]
    assert token_mount["mountPath"] == "/data/instance_token"
    assert token_mount["subPath"] == "instance_token"
    assert token_mount.get("readOnly") is True
    assert any(m["mountPath"] == "/data" for m in container["volumeMounts"]), "数据根必须挂卷"


def test_container_runs_hardened_and_non_root():
    """⑩ 安全上下文：非 root、根文件系统只读、无提权、能力全丢。"""
    pod_spec = _by_kind("Deployment")["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    container_sc = pod_spec["containers"][0]["securityContext"]
    assert container_sc["allowPrivilegeEscalation"] is False
    assert container_sc["readOnlyRootFilesystem"] is True
    assert container_sc["capabilities"]["drop"] == ["ALL"]


def test_writable_paths_are_explicit_and_not_a_home_dir():
    """⑩b 只读根 FS 下唯一可写面：/data（卷）与显式缓存目录；**不许出现 /home/<用户> 形态**。

    两个理由：① 只读根 FS 下 `$HOME/.cache` 不可写，缓存必须显式外挂；
    ② `/home/<名字>` 会被公开面泄露闸的「POSIX 用户主目录路径」模式误判成本机家目录
    （`scripts/scan_public_leak.py` 的 `[/\\\\]home[/\\\\][a-z][a-z0-9_-]{2,}`），
    进 CI 就是一条红。所以缓存目录取 /opt/hc-cache 并用 XDG_CACHE_HOME／HF_HOME 指过去。
    """
    container = _by_kind("Deployment")["spec"]["template"]["spec"]["containers"][0]
    mounts = [m["mountPath"] for m in container["volumeMounts"]]
    assert "/data" in mounts
    for path in mounts:
        assert not re.search(r"/home/[a-z][a-z0-9_-]{2,}", path), f"挂载点命中 /home/<用户> 形态：{path}"
    config = _by_kind("ConfigMap")["data"]
    assert config.get("XDG_CACHE_HOME") and config.get("HF_HOME"), "缓存目录没显式外指（只读根 FS 下会写失败）"
    for key in ("XDG_CACHE_HOME", "HF_HOME"):
        assert config[key] in mounts, f"{key}={config[key]} 没有对应的挂载点"


# ----------------------------------------------------------------------
# Dockerfile：密钥不进镜像（与 Secret 清单侧是同一根链条的两端）
# ----------------------------------------------------------------------

FORBIDDEN_COPY = (".env", "*.pem", "*.key", "instance_token", "data/", "config.json")


def test_dockerfile_is_multistage():
    """⑪ 多阶段：≥2 个 FROM，且 builder 与 runtime 都有名字（阶段名是层复用的锚点）。"""
    stages = [line.strip() for line in DOCKERFILE.read_text(encoding="utf-8").splitlines() if line.strip().upper().startswith("FROM ")]
    assert len(stages) >= 2, f"不是多阶段构建：{stages}"
    assert sum(" AS builder" in s for s in stages) == 1
    assert sum(" AS runtime" in s for s in stages) == 1


def test_dockerfile_never_copies_secretish_paths():
    """⑫ COPY 清单里不许出现凭据/数据根/配置文件名（镜像层会留痕）。"""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        for bad in FORBIDDEN_COPY:
            assert bad not in stripped, f"COPY 命中敏感路径 {bad!r}：{stripped}"


def test_dockerfile_env_lines_have_no_literal_credentials():
    """⑬ ENV/ARG 里凡按名字像凭据的，右侧必须为空（不许把 key 写死在镜像里）。"""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not re.match(r"(ENV|ARG)\s", stripped):
            continue
        for token in re.findall(r"([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)=(.*)", stripped, re.I):
            name, value = token
            assert not value.strip(), f"{name} 在 Dockerfile 里被赋了非空值（密钥会进镜像层）"


def test_dockerfile_layers_deps_before_source_and_avoids_pip_cache():
    """⑭ 依赖分层（先清单后源码）＋不落 pip 缓存 —— 分层缓存与体积两条一起验。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    deps_at = text.index('RUN python -m venv /opt/venv')
    src_at = text.index("COPY src/ ./src/")
    assert deps_at < src_at, "依赖必须在拷源码之前装，否则改一行代码就要重装全部依赖"
    assert "PIP_NO_CACHE_DIR=1" in text
    assert ".venv" in text or "venv /opt/venv" in text


def test_dockerfile_runtime_is_hardened_and_has_healthcheck():
    """⑮ 运行阶段：非 root、数据根挂卷、HEALTHCHECK 带令牌（与 Deployment 探针同口径）。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^USER\s+10001", text, re.M), "容器必须以非 root 用户运行"
    assert 'VOLUME ["/data"]' in text
    assert "HEALTHCHECK" in text and "Authorization" in text, "HEALTHCHECK 没带令牌 → 非环回下永远不健康"


def test_dockerignore_keeps_credentials_and_state_out_of_context():
    """⑯ .dockerignore 必须挡住凭据与数据根（构建上下文会进 daemon）。"""
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    for pattern in (".env", "*token*", ".hippocampus/", "*.db", ".venv/"):
        assert pattern in text, f".dockerignore 少了 {pattern}"


@pytest.mark.parametrize("name", ["40-deployment.yaml", "20-secret.example.yaml"])
def test_key_manifests_are_present(name: str):
    """⑰ 关键清单在盘（防"文件被删但测试还绿"）。"""
    assert (K8S_DIR / name).is_file()
