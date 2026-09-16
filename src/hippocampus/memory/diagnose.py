# 由前身 hippocampus_prototype/diagnose.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus 反馈环 N4 -- 完整诊断链

四问定位 → 根因 → 归因 → 标准修复处方 → 验证+防复发
当重述警报触发时，自动诊断"记忆断在哪一环"。

诊断四问：
  Q1 库里有吗 -- 检索到的记忆是否在 SQLite memories 表
  Q2 检索到了吗 -- 重跑 retrieve 看是否命中
  Q3 注入了吗 -- 命中的记忆是否在 top-8 最终注入列表
  Q4 内容对吗 -- 注入记忆的 source_quote 与原始经历对比
"""

import sqlite3

from hippocampus.memory import feedback as fb
from hippocampus.memory import retrieval as rt


def diagnose_recall_failure(
    conn: sqlite3.Connection,
    user_text: str,
    collection=None,
    index=None,
    topic_fp: str = "",
) -> dict:
    """诊断链主入口：对一条重复的 user_text 跑四问诊断。

    返回 dict：
    {
        "locate": "Q1|Q2|Q3|Q4|ok",      # 定位：断在哪一环
        "root_cause": str,                 # 根因
        "attribution": str,                # 归因（偶发/系统性）
        "fix": str,                        # 标准修复处方
        "summary": str,                    # 摘要（拼 reply_text 尾）
        "detail": dict,                    # 完整诊断细节
    }
    """
    # collection 参数兼容：双池 dict / 单 collection 对象（旧调用方）/ None（默认双池）
    if isinstance(collection, dict):
        collections = collection
    elif collection is None:
        collections = rt._default_collections()
    else:
        collections = {"mem": collection, "ep": collection}
    if index is None:
        index = rt.build_bm25(conn)

    detail = {"user_text": user_text[:200], "topic_fp": topic_fp}

    # ---- Q1: 库里有吗 ----
    # 双池分别查：mem 池看记忆、ep 池看经历（N3 重述检测同口径）
    semantic_mem = rt.semantic_search(collections["mem"], user_text, n=rt.TOP_K * rt.OVER_FETCH)
    semantic_ep = rt.semantic_search(collections["ep"], user_text, n=rt.TOP_K * rt.OVER_FETCH)
    all_mem_ids = [doc_id for doc_id, v in semantic_mem.items() if v.get("kind") == "memory"]
    all_ep_ids = [doc_id for doc_id, v in semantic_ep.items() if v.get("kind") == "episode"]
    detail["Q1_semantic_hits"] = {
        "memories": len(all_mem_ids),
        "episodes": len(all_ep_ids),
        "threshold": rt.SEMANTIC_THRESHOLD,
    }

    if not all_mem_ids and not all_ep_ids:
        result = {
            "locate": "Q1",
            "root_cause": "库中无相关记忆或经历（语义搜索零命中）",
            "attribution": "偶发" if _count_similar_diagnoses(conn, topic_fp) < 2 else "系统性",
            "fix": "检查提取入库流程是否遗漏，或语义阈值过高",
            "summary": "> 记忆·异常：你上回提过这件事，但这回没接上：上回的内容没找到，可能上次忘了记。已记录，我会跟进。",
            "detail": detail,
        }
        _log_diagnosis(conn, topic_fp, result)
        return result

    # ---- Q2: 检索到了吗（重跑 retrieve，双池）----
    retrieved = rt.retrieve(conn, user_text, collections, index, top_k=8)
    retrieved_ids = [r["doc_id"] for r in retrieved["results"]]
    retrieved_mem_ids = [r["doc_id"] for r in retrieved["results"] if r["kind"] == "memory"]
    detail["Q2_retrieve"] = {
        "final_count": retrieved["channels"]["final_count"],
        "retrieved_ids": retrieved_ids[:8],
        "channels": {
            k: v for k, v in retrieved["channels"].items() if k in ("semantic_hits", "bm25_hits", "graph_hits")
        },
    }

    if not retrieved_mem_ids:
        result = {
            "locate": "Q2",
            "root_cause": "语义有命中但融合排序后记忆被过滤（可能分数低于阈值或被 superseded 过滤）",
            "attribution": "偶发" if _count_similar_diagnoses(conn, topic_fp) < 2 else "系统性",
            "fix": "检查融合排序阈值或 superseded 过滤逻辑",
            "summary": "> 记忆·异常：你上回提过这件事，但这回没接上：找到了几条相关的，但排序后被挤掉了。已记录，我会跟进。",
            "detail": detail,
        }
        _log_diagnosis(conn, topic_fp, result)
        return result

    # ---- Q3: 注入了吗（在 top-8 里吗）----
    top8 = retrieved["results"][:8]
    top8_mem = [r for r in top8 if r["kind"] == "memory"]
    detail["Q3_injection"] = {
        "top8_count": len(top8),
        "top8_mem_count": len(top8_mem),
        "top8_ids": [r["doc_id"] for r in top8],
    }

    if not top8_mem:
        result = {
            "locate": "Q3",
            "root_cause": "记忆通过检索但未进 top-8 注入列表（排名太靠后）",
            "attribution": "偶发" if _count_similar_diagnoses(conn, topic_fp) < 2 else "系统性",
            "fix": "考虑调整 top_k 或注入策略",
            "summary": "> 记忆·异常：你上回提过这件事，但这回没接上：检索到了，但没送进给模型的注入列表。已记录，我会跟进。",
            "detail": detail,
        }
        _log_diagnosis(conn, topic_fp, result)
        return result

    # ---- Q4: 内容对吗 ----
    # 取注入的第一条记忆，对比 source_quote 与原始经历
    first_mem_id = top8_mem[0]["doc_id"]
    mem_row = conn.execute("SELECT * FROM memories WHERE id=?", (first_mem_id,)).fetchone()
    if not mem_row:
        result = {
            "locate": "Q4",
            "root_cause": "记忆 ID 在 memories 表中不存在（数据不一致）",
            "attribution": "偶发",
            "fix": "检查数据库完整性",
            "summary": "> 记忆·异常：你上回提过这件事，但这回没接上：记忆记录找不到了。已记录，我会跟进。",
            "detail": detail,
        }
        _log_diagnosis(conn, topic_fp, result)
        return result

    mem_dict = dict(mem_row)
    detail["Q4_content"] = {
        "memory_id": first_mem_id,
        "memory_content": mem_dict["content"][:100],
        "source_quote": mem_dict["source_quote"][:100],
        "source_episode_id": mem_dict["source_episode_id"],
        "created_at": mem_dict["created_at"],
    }

    # 如果有 source_quote，检查原始经历是否一致
    if mem_dict["source_episode_id"]:
        ep_row = conn.execute("SELECT * FROM episodes WHERE id=?", (mem_dict["source_episode_id"],)).fetchone()
        if ep_row:
            detail["Q4_content"]["episode_content"] = ep_row["content"][:100]
            # 简单一致性检查：source_quote 是否出现在原始经历中
            sq = mem_dict["source_quote"]
            ec = ep_row["content"]
            if sq and sq not in ec and ec not in sq:
                # 内容不一致
                result = {
                    "locate": "Q4",
                    "root_cause": f"记忆的 source_quote 与原始经历内容不一致（记忆ID={first_mem_id}）",
                    "attribution": "偶发" if _count_similar_diagnoses(conn, topic_fp) < 2 else "系统性",
                    "fix": "检查 LLM 提取流程是否产生幻觉",
                    "summary": "> 记忆·异常：你上回提过这件事，但这回没接上：内容对不上。已记录，我会跟进。",
                    "detail": detail,
                }
                _log_diagnosis(conn, topic_fp, result)
                return result

    # ---- 全部通过：记忆链路正常 ----
    result = {
        "locate": "ok",
        "root_cause": "记忆链路正常，四问全通过",
        "attribution": "无",
        "fix": "无需修复",
        "summary": "> 记忆·复查：你上回提过这件事，我检查了记忆——存了、找得到、也用上了，一切正常。",
        "detail": detail,
    }
    _log_diagnosis(conn, topic_fp, result)
    return result


def _count_similar_diagnoses(conn: sqlite3.Connection, topic_fp: str) -> int:
    """统计同一主题指纹的历史 diagnosis 事件数（用于归因：≥2=系统性）。"""
    if not topic_fp:
        return 0
    events = fb.query_events_by_topic(conn, topic_fp)
    return sum(1 for e in events if e["event_type"] == "diagnosis")


def _log_diagnosis(conn: sqlite3.Connection, topic_fp: str, result: dict) -> str:
    """把诊断结果写入 feedback_logs。"""
    attribution_full = result["attribution"]
    if attribution_full == "系统性":
        attribution_full = "系统性（同类≥2次）→ 建议改 Prompt/参数校准/注入策略"

    return fb.log_event(
        conn,
        event_type="diagnosis",
        topic_fp=topic_fp,
        locate=result["locate"],
        root_cause=result["root_cause"],
        attribution=attribution_full,
        fix=result["fix"],
        verify_result="",
        prevent_result=f"30天窗口监控（{int(__import__('time').time())}）",
        extra=result["detail"],
    )
