# 由前身 hippocampus_prototype/embedding_models.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
embedding_models.py — 可插拔 embedding 模型加载器（任务书 P02 任务 1）

配置格式（前缀制，config.py embedding 节 model 字段）：
- 裸名（无冒号）→ chromadb 内置逻辑（与 8c 行为逐字节一致：默认短路 /
  内置 embedding_functions 查找 / 未知模型 warning + 回退默认）
- "onnx:<HF 仓库名>" → ONNX 加载器（tokenizers + onnxruntime 本地推理，
  零新增依赖——打包产物 dist/Hippocampus/_internal/ 已含两库）
- "sentence_transformer:<HF 仓库名>" → stderr 提示缺依赖 + 回退默认（不安装）

模型文件下载到 runtime.data_root()/models/<仓库名>/（懒加载：首次调用才下载）。
支持 HF_ENDPOINT 环境变量换镜像（如 https://hf-mirror.com），下载失败自动
换镜像重试。model.onnx 按 HF LFS sha256（API lfs.oid）校验。
任何下载/加载/推理失败 → stderr 警告 + 内部回退 chromadb 内置
ONNXMiniLM_L6_V2（默认模型，绝不崩）。

查询指令前缀：bge 官方用法「查询侧加指令、文档侧不加」。ONNXEmbeddingFunction
的 embed_query（chroma query_texts 路径 + retrieval 手动 query 路径）自动加
query_instruction；__call__（add/upsert/文档侧）不加。
"""

import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

from hippocampus.memory import runtime

# bge 官方检索指令（BAAI/bge-small-zh-v1.5 Model List 逐字）
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

# 查询侧需加检索指令的 ONNX 中文模型（s2p 检索任务，bge 官方用法）
_QUERY_INSTRUCTION_BY_MODEL = {
    "Xenova/bge-small-zh-v1.5": BGE_QUERY_INSTRUCTION,
}

# 必需下载文件（不含量化变体 model_bnb4/model_fp16/model_int8/model_q4 等）
_REQUIRED_FILES = (
    "config.json",
    "onnx/model.onnx",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)

# 下载端点轮换顺序：HF_ENDPOINT 环境变量（若有）→ 官方 → hf-mirror 镜像
_HF_ENDPOINTS = ("https://huggingface.co", "https://hf-mirror.com")

_MAX_LENGTH = 384  # 序列长度上限（见 `_encode` 的说明）


# ---------- 下载 ----------


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _http_get(url: str, timeout: int = 60) -> bytes:
    if _HTTP_FETCHER is not None:
        return _HTTP_FETCHER(url, timeout)  # 注入式抓取器（URL 已被调用方校验）
    req = urllib.request.Request(url, headers={"User-Agent": "hippocampus-p02/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_lfs_sha(endpoint: str, repo: str) -> dict:
    """从 HF API 拿 LFS 文件 sha256（{path: oid}）；API 异常返回 {}（跳过校验）。"""
    try:
        url = _validate_model_url(f"{endpoint}/api/models/{repo}/tree/main?recursive=true", endpoint)
        data = json.loads(_http_get(url, timeout=30).decode("utf-8"))
        out = {}
        for item in data:
            lfs = item.get("lfs") or {}
            if lfs.get("oid"):
                out[item["path"]] = lfs["oid"]
        return out
    except Exception:
        return {}


# ---- 出站校验与注入式抓取器（安全审计 N20-③，A41 口径）----
# 结构与 `agent/tools.py::_fetch_url` 同款：**先校验、再交给抓取器**。
# 本模块的 URL 由「配置端点 + 仓库名」拼出，两段都要过闸：
#   · 端点：只允许 http/https（`validate_endpoint_url`；本地镜像/环回是操作员显式配置的，允许）；
#   · 仓库名：白名单 `[A-Za-z0-9._-]`（不允许 `/`、`@`、`:`，因此无法改主机或跨路径）；
#   · 最终 URL 的主机必须与端点主机一致（防"仓库名里塞主机"把请求引到别处）。
# HF 仓库名是 `org/name` 或裸 `name`：两段都只允许 `[A-Za-z0-9._-]`，且每段不以 `.` 开头
_REPO_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")

# 抓取器可注入（测试与受限环境用）；None 时走模块内默认实现
_HTTP_FETCHER: Callable[[str, int], bytes] | None = None


def set_http_fetcher(fn: Callable[[str, int], bytes] | None) -> None:
    """注入出站抓取器：`fn(url, timeout) -> bytes`。传入 None 恢复默认实现。

    为什么要有：出站能力可被替换/关闭（受限环境、测试里断言"没有出站"），
    且校验永远发生在**交给抓取器之前**（与 agent 工具同一条纪律）。
    """
    global _HTTP_FETCHER
    _HTTP_FETCHER = fn


def _safe_repo(repo: str) -> str:
    """仓库名白名单（路径穿越与主机注入的第一道闸）。

    只接受 `name` 或 `org/name`（各段 `[A-Za-z0-9._-]`、不以点开头、不含 `..`）——
    因此 `/`、`@`、`:`、`..` 都进不来：既不能跨出 models 目录，也不能改 URL 主机。
    """
    name = str(repo or "")
    parts = name.split("/")
    if len(parts) > 2 or not parts or any(not _REPO_SEGMENT_RE.fullmatch(seg) for seg in parts):
        raise ValueError(f"非法仓库名: {name!r}")
    if any(".." in seg for seg in parts):
        raise ValueError(f"非法仓库名（含 ..）: {name!r}")
    return "/".join(parts)


def _validate_model_url(url: str, endpoint: str = "") -> str:
    """只允许 http/https；主机必须与端点一致；拒环回/私有/保留（端点自身例外）。"""
    from urllib.parse import urlparse

    from hippocampus.net import validate_endpoint_url

    safe = validate_endpoint_url(url)
    if endpoint:
        want = urlparse(endpoint).hostname
        got = urlparse(safe).hostname
        if want and got != want:
            raise ValueError(f"出站主机与端点不一致: {got!r} != {want!r}")
    return safe


def _endpoints() -> list:
    eps = []
    env = os.environ.get("HF_ENDPOINT")
    if env:
        eps.append(env.rstrip("/"))
    eps.extend(e for e in _HF_ENDPOINTS if e not in eps)
    return eps


def _model_dir(repo: str) -> Path:
    """模型落盘目录。打比方：它是"公共工具库"，不是"谁的私人书架"——模型文件是所有
    记忆库共享的只读资源，所以**永远放在全局默认数据根**，不跟随当前 core 的
    `--home`。这保证：① 换 `--home` 不会重复下载几百兆；② 基准评测的临时数据根
    （`--home D:/tmp/hc-bench/…`）不需要网络也能用已下好的模型。

    安全（N20）：`runtime.default_data_root()` 是**机器级固定路径**，不含用户输入，
    与仓库名白名单（`_safe_repo`）一起保证落盘路径不可被外部串改写。
    """
    return runtime.default_data_root() / "models" / _safe_repo(repo)


def _download_repo(repo: str) -> Path:
    """下载必需文件到 models/<repo>/；model.onnx 按 API lfs.oid 校验 sha256。

     全部端点失败抛 RuntimeError（调用方回退默认）。半成品跨端点清理。

    安全（N20-③）：仓库名过白名单；每个下载 URL 先过 `_validate_model_url`
    （只 http/https、主机必须等于端点主机）。"""
    repo = _safe_repo(repo)
    target = _model_dir(repo)
    if all((target / f).exists() for f in _REQUIRED_FILES):
        return target
    target.mkdir(parents=True, exist_ok=True)

    expected_sha = {}
    last_err = None
    for ep in _endpoints():
        try:
            if not expected_sha:
                expected_sha = _fetch_lfs_sha(ep, repo)
            for rel in _REQUIRED_FILES:
                dst = target / rel
                if dst.exists():
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)  # 子目录（onnx/）也要建
                url = _validate_model_url(f"{ep}/{repo}/resolve/main/{rel}", ep)
                tmp = target / (rel + ".tmp")
                _http_download(url, tmp)
                if rel == "onnx/model.onnx" and expected_sha.get(rel):
                    if _sha256(tmp) != expected_sha[rel]:
                        tmp.unlink(missing_ok=True)
                        raise RuntimeError(f"sha256 校验失败 {rel}: {_sha256(tmp)[:12]} != {expected_sha[rel][:12]}")
                os.replace(tmp, dst)
            return target
        except Exception as e:
            last_err = e
            for rel in _REQUIRED_FILES:
                (target / rel).unlink(missing_ok=True)
            print(f"[warning] embedding 下载端点 {ep} 失败: {e}", file=sys.stderr)
    raise RuntimeError(f"embedding 模型 {repo} 下载失败（全部端点）: {last_err}")


def _http_download(url: str, dst: Path, timeout: int = 180) -> None:
    if _HTTP_FETCHER is not None:
        dst.write_bytes(_HTTP_FETCHER(url, timeout))  # 注入式抓取器（URL 已被调用方校验）
        return
    req = urllib.request.Request(url, headers={"User-Agent": "hippocampus-p02/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dst, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


# ---------- ONNX 加载器 ----------


class ONNXEmbeddingFunction:
    """ONNX 本地 embedding：tokenizers 编码 + onnxruntime 推理 + CLS pooling + L2 归一化。

    懒加载：首次 __call__/embed_query 才下载模型 + 加载 tokenizer/会话。
    下载/加载/推理失败：stderr 警告 + 回退 chromadb 内置 ONNXMiniLM_L6_V2，绝不崩。
    query_instruction 非空时 embed_query（查询侧）自动加指令；__call__（文档侧）不加。
    """

    def __init__(self, repo: str):
        self.repo = repo
        self.query_instruction = _QUERY_INSTRUCTION_BY_MODEL.get(repo, "")
        self._tokenizer = None
        self._session = None
        self._fallback = None
        self._warned = False

    # ---------- 懒加载 ----------

    def _ensure_loaded(self) -> None:
        """下载 + 加载；失败记录 _warned（首次警告），调用方走回退。"""
        if self._session is not None:
            return
        try:
            model_dir = _download_repo(self.repo)
            from tokenizers import Tokenizer as Tok

            tok = Tok.from_file(str(model_dir / "tokenizer.json"))
            pad_id = tok.token_to_id("[PAD]")
            if pad_id is None:
                pad_id = 0
            try:
                tok.enable_truncation(max_length=_MAX_LENGTH)
                tok.enable_padding(pad_id=pad_id, pad_token="[PAD]")
            except Exception:
                pass
            self._tokenizer = tok

            import onnxruntime

            onnx_path = model_dir / "onnx" / "model.onnx"
            self._session = onnxruntime.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
            # 预热一次（确认输入名/输出形状，把慢加载放首次调用内）
            self._encode(["预热"])
        except Exception as e:
            if not self._warned:
                print(
                    f"[warning] ONNX embedding 模型 '{self.repo}' 加载失败，回退默认 onnx_mini_lm_l6_v2: {e}",
                    file=sys.stderr,
                )
                self._warned = True
            self._session = None

    def _encode(self, texts: list) -> list:
        """ONNX 推理。内存纪律写清楚（实测教训）：

        - tokenizer 侧已 `enable_truncation(max_length=384)`，理论上不会有超长序列；
          但仍在推理前做一次**硬截断**（按分词后长度切到 384），防止"某次配置被改掉"
          或"别的 tokenizer 路径"把长序列送进来——实测症状是 onnxruntime 的
          Rust 侧 `memory allocation of 2097152 bytes failed` **直接把进程炸掉**
          （不是抛异常，是进程崩，没法 catch）。
        - tokenizers 的 `encode_batch` 对长文本的内存是序列长度的**平方级**；
          384 的上限是"召回不掉点"与"不爆内存"的折中（bge 类检索任务，384 足够）。
        """
        encs = self._tokenizer.encode_batch(texts)
        import numpy as np

        for enc in encs:
            if len(enc.ids) > _MAX_LENGTH:
                enc.truncate(_MAX_LENGTH)
        feed = {}
        for inp in self._session.get_inputs():
            name = inp.name
            if name == "input_ids":
                feed[name] = np.array([e.ids for e in encs], dtype=np.int64)
            elif name == "attention_mask":
                feed[name] = np.array([e.attention_mask for e in encs], dtype=np.int64)
            elif name == "token_type_ids":
                feed[name] = np.array([e.type_ids for e in encs], dtype=np.int64)
        out = self._session.run(None, feed)[0]  # [batch, seq, hidden]
        emb = out[:, 0, :]  # CLS pooling（bge 官方）
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        emb = emb / norms  # L2 归一化（cosine 空间）
        return [row.tolist() for row in emb]

    def _get_fallback(self):
        if self._fallback is None:
            from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

            self._fallback = ONNXMiniLM_L6_V2()
        return self._fallback

    # ---------- chromadb EmbeddingFunction 协议 ----------

    def __call__(self, input):
        """文档侧编码（不加检索指令）。input: List[str]。"""
        if isinstance(input, str):
            input = [input]
        if not input:
            return []
        self._ensure_loaded()
        if self._session is None:
            return self._get_fallback()(input)
        try:
            return self._encode(list(input))
        except Exception as e:
            if not self._warned:
                print(
                    f"[warning] ONNX embedding '{self.repo}' 推理失败，回退默认 onnx_mini_lm_l6_v2: {e}",
                    file=sys.stderr,
                )
                self._warned = True
            return self._get_fallback()(input)

    def embed_query(self, input):
        """查询侧编码（自动加官方检索指令前缀——bge s2p 用法）。"""
        if isinstance(input, str):
            input = [input]
        if self.query_instruction:
            input = [self.query_instruction + t for t in input]
        return self(input)

    def name(self) -> str:
        return "onnx:" + self.repo

    def get_config(self) -> dict:
        return {"repo": self.repo, "query_instruction": self.query_instruction}

    def max_tokens(self) -> int:
        return _MAX_LENGTH


# ---------- 前缀分发 ----------


def _resolve_chromadb_builtin(model: str):
    """裸模型名 → chromadb 内置 embedding_functions 查找（8c 原逻辑原样搬移）。

    默认名（onnx_mini_lm_l6_v2 或空）返回 None（不传 = chromadb 默认，现状零改动）；
    未知裸名 → stderr warning + 返回 None（回退默认不崩，8c 验收 D3 依赖此行为）。"""
    if not model or model == "onnx_mini_lm_l6_v2":
        return None
    try:
        import chromadb.utils.embedding_functions as ef
    except Exception:
        return None
    raw = getattr(ef, model, None)
    if raw is None:
        print(
            f"[warning] embedding 模型 '{model}' 不在 chromadb 内置 "
            f"embedding_functions 中，回退默认 onnx_mini_lm_l6_v2"
            f"（默认行为不变）",
            file=sys.stderr,
        )
        return None
    try:
        return raw()
    except Exception:
        print(
            f"[warning] embedding 模型 '{model}' 实例化失败，回退默认 onnx_mini_lm_l6_v2（默认行为不变）",
            file=sys.stderr,
        )
        return None


def resolve(model: str):
    """按配置模型名解析 embedding 函数（任务书 P02 任务 1 前缀分发）。

    - 裸名（无冒号）→ chromadb 内置逻辑（与 8c 行为逐字节一致）
    - "onnx:<HF 名>" → ONNXEmbeddingFunction（懒加载，失败内部回退不崩）
    - "sentence_transformer:<HF 名>" → stderr 提示缺依赖 + 回退默认
    - 未知前缀 → warning + 回退默认
    """
    if not model or ":" not in model:
        return _resolve_chromadb_builtin(model)
    prefix, _, hf_name = model.partition(":")
    if prefix == "onnx":
        if not hf_name:
            print("[warning] onnx: 前缀缺模型名，回退默认 onnx_mini_lm_l6_v2", file=sys.stderr)
            return None
        return ONNXEmbeddingFunction(hf_name)
    if prefix == "sentence_transformer":
        print(
            f"[warning] embedding 模型 '{hf_name}' 需要 sentence-transformers"
            f"（pip install sentence-transformers），回退默认 "
            f"onnx_mini_lm_l6_v2（默认行为不变）",
            file=sys.stderr,
        )
        return None
    print(f"[warning] 未知 embedding 前缀 '{prefix}:'，回退默认 onnx_mini_lm_l6_v2（默认行为不变）", file=sys.stderr)
    return None
