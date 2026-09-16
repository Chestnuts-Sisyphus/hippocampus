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
import sys
import urllib.request
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

_MAX_LENGTH = 512  # bge 系列序列长度上限


# ---------- 下载 ----------


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "hippocampus-p02/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_lfs_sha(endpoint: str, repo: str) -> dict:
    """从 HF API 拿 LFS 文件 sha256（{path: oid}）；API 异常返回 {}（跳过校验）。"""
    try:
        url = f"{endpoint}/api/models/{repo}/tree/main?recursive=true"
        data = json.loads(_http_get(url, timeout=30).decode("utf-8"))
        out = {}
        for item in data:
            lfs = item.get("lfs") or {}
            if lfs.get("oid"):
                out[item["path"]] = lfs["oid"]
        return out
    except Exception:
        return {}


def _endpoints() -> list:
    eps = []
    env = os.environ.get("HF_ENDPOINT")
    if env:
        eps.append(env.rstrip("/"))
    eps.extend(e for e in _HF_ENDPOINTS if e not in eps)
    return eps


def _model_dir(repo: str) -> Path:
    return runtime.data_root() / "models" / repo


def _download_repo(repo: str) -> Path:
    """下载必需文件到 models/<repo>/；model.onnx 按 API lfs.oid 校验 sha256。

    全部端点失败抛 RuntimeError（调用方回退默认）。半成品跨端点清理。"""
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
                url = f"{ep}/{repo}/resolve/main/{rel}"
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
        encs = self._tokenizer.encode_batch(texts)
        import numpy as np

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
