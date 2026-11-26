"""标讯中心：列表/详情/AI 解读/手动导入；匹配结果处理；采集来源管理。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import db, jobs, llm, matching
from ..config import SNAPSHOT_DIR, now_str
from ..crawler import ccgp, runner
from ..crawler.base import RobotsDisallowed, StopCrawl
from ..extract import TYPE_LABELS
from ..security import current_user, require
from . import must, pick

router = APIRouter(prefix="/api", tags=["notices"])


def _notice_query(
    company_id: int = 0,
    only_matched: int = 1,
    level: str = "",
    status: str = "",
    notice_type: str = "",
    region: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    hide_expired: int = 0,
) -> tuple[str, list[str], list]:
    """列表、批量删除、按筛选清空共用同一套筛选条件，保证「看到的就是删掉的」。"""
    where = ["n.dup_of IS NULL"]
    params: list = []
    join = ""
    if company_id or only_matched:
        join = "JOIN matches m ON m.notice_id = n.id JOIN companies c ON c.id = m.company_id"
        if company_id:
            where.append("m.company_id = ?")
            params.append(company_id)
        if level:
            where.append("m.level = ?")
            params.append(level)
        if status:
            where.append("m.status = ?")
            params.append(status)
        else:
            where.append("m.status != 'ignored'")
    if notice_type:
        where.append("n.notice_type = ?")
        params.append(notice_type)
    if region:
        where.append("(n.region LIKE ? OR n.purchaser LIKE ?)")
        params += [f"%{region}%", f"%{region}%"]
    if q:
        where.append("(n.title LIKE ? OR n.purchaser LIKE ? OR n.project_no LIKE ?)")
        params += [f"%{q}%"] * 3
    if date_from:
        where.append("n.publish_time >= ?")
        params.append(date_from)
    if date_to:
        where.append("n.publish_time <= ?")
        params.append(date_to + " 23:59")
    if hide_expired:
        where.append("(n.deadline = '' OR n.deadline >= ?)")
        params.append(now_str()[:16])
    return join, where, params


def _delete_notice_ids(ids: list[int], user: dict) -> dict:
    """删除公告、对应匹配记录和原文快照。已被投标项目引用的跳过，避免项目丢来源。"""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {"deleted": 0, "skipped": 0, "skipped_ids": []}
    ph = ",".join("?" * len(ids))
    used = {
        r["notice_id"]
        for r in db.q(f"SELECT DISTINCT notice_id FROM projects WHERE notice_id IN ({ph})", ids)
        if r["notice_id"]
    }
    targets = [i for i in ids if i not in used]
    deleted = 0
    if targets:
        tph = ",".join("?" * len(targets))
        deleted = int(db.scalar(f"SELECT COUNT(*) FROM notices WHERE id IN ({tph})", targets) or 0)
        if deleted:
            db.ex(f"DELETE FROM matches WHERE notice_id IN ({tph})", targets)
            db.ex(f"UPDATE notices SET dup_of = NULL WHERE dup_of IN ({tph})", targets)
            db.ex(f"DELETE FROM notices WHERE id IN ({tph})", targets)
            for i in targets:
                try:
                    (SNAPSHOT_DIR / f"{i}.html").unlink(missing_ok=True)
                except OSError:
                    pass
    db.audit(user, "删除公告", f"删除 {deleted} 条，跳过已立项 {len(used)} 条")
    return {"deleted": deleted, "skipped": len(used), "skipped_ids": sorted(used)}


def _match_out(m: dict | None) -> dict | None:
    if not m:
        return None
    m["reasons"] = db.jload(m.pop("reasons_json", "[]"), [])
    m["missing"] = db.jload(m.pop("missing_json", "[]"), [])
    return m


@router.get("/notices")
def list_notices(
    company_id: int = 0,
    only_matched: int = 1,
    level: str = "",
    status: str = "",
    notice_type: str = "",
    region: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    hide_expired: int = 0,
    sort: str = "score",
    page: int = 1,
    size: int = 30,
    user: dict = Depends(current_user),
):
    size = max(5, min(100, size))
    join, where, params = _notice_query(
        company_id, only_matched, level, status, notice_type, region, q, date_from, date_to, hide_expired
    )
    order = {
        "score": "m.score DESC, n.publish_time DESC" if join else "n.publish_time DESC",
        "publish": "n.publish_time DESC",
        "deadline": "CASE WHEN n.deadline = '' THEN 1 ELSE 0 END, n.deadline ASC",
        "budget": "n.budget IS NULL, n.budget DESC",
    }.get(sort, "n.publish_time DESC")
    cols = (
        "n.id, n.title, n.notice_type, n.type_label, n.publish_time, n.region, n.purchaser, n.budget, n.deadline, "
        "n.source_name, n.detail_fetched, n.category, n.url"
    )
    if join:
        cols += (
            ", m.id AS m_id, m.company_id AS m_company_id, c.name AS m_company_name, c.short_name AS m_company_short, m.score AS m_score, m.level AS m_level, "
            "m.level_by AS m_level_by, m.reasons_json AS m_reasons_json, m.missing_json AS m_missing_json, m.status AS m_status, m.keyword_hits AS m_keyword_hits"
        )
    base = f"FROM notices n {join} WHERE {' AND '.join(where)}"
    total = db.scalar(f"SELECT COUNT(*) {base}", params)
    rows = db.q(f"SELECT {cols} {base} ORDER BY {order} LIMIT ? OFFSET ?", params + [size, (page - 1) * size])
    items = []
    for r in rows:
        it = {k: v for k, v in r.items() if not k.startswith("m_")}
        it["type_name"] = TYPE_LABELS.get(r["notice_type"], r["notice_type"])
        if join:
            it["match"] = {
                "id": r["m_id"],
                "company_id": r["m_company_id"],
                "company_name": r["m_company_short"] or r["m_company_name"],
                "score": r["m_score"],
                "level": r["m_level"],
                "level_by": r["m_level_by"],
                "reasons": db.jload(r["m_reasons_json"], []),
                "missing": db.jload(r["m_missing_json"], []),
                "status": r["m_status"],
                "keyword_hits": r["m_keyword_hits"],
            }
        items.append(it)
    return {"total": total, "page": page, "size": size, "items": items}


@router.get("/notices/{nid}")
def get_notice(nid: int, user: dict = Depends(current_user)):
    n = must(db.get("notices", nid), "标讯")
    n["attachments"] = db.jload(n.pop("attachments_json"), [])
    n["winners"] = db.jload(n.pop("winners_json"), [])
    n["ai_summary"] = db.jload(n.pop("ai_summary_json"), None)
    n["type_name"] = TYPE_LABELS.get(n["notice_type"], n["notice_type"])
    n["matches"] = [
        _match_out(m)
        for m in db.q(
            "SELECT m.*, c.name AS company_name, c.short_name AS company_short FROM matches m JOIN companies c ON c.id = m.company_id WHERE m.notice_id = ? ORDER BY m.score DESC",
            (nid,),
        )
    ]
    n["duplicates"] = db.q(
        "SELECT id, title, source_name, url, publish_time FROM notices WHERE dup_of = ? OR id = ? ",
        (nid, n.get("dup_of") or -1),
    )
    n["projects"] = db.q("SELECT id, name, company_id, status FROM projects WHERE notice_id = ?", (nid,))
    history = []
    if n.get("purchaser"):
        for h in db.q(
            "SELECT id, title, publish_time, winners_json, award_amount FROM notices WHERE purchaser = ? AND notice_type = 'award' AND id != ? "
            "ORDER BY publish_time DESC LIMIT 10",
            (n["purchaser"], nid),
        ):
            h["winners"] = db.jload(h.pop("winners_json"), [])
            history.append(h)
    n["purchaser_history"] = history
    return n


@router.post("/notices/{nid}/fetch_detail")
def refetch_detail(nid: int, user: dict = Depends(current_user)):
    must(db.get("notices", nid), "标讯")
    try:
        runner.fetch_detail(nid)
    except (StopCrawl, RobotsDisallowed) as e:
        raise HTTPException(429, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"抓取失败：{type(e).__name__} {e}") from e
    matching.match_notice(nid)
    runner.link_results(nid)
    return get_notice(nid, user)


@router.post("/notices/{nid}/ai_summary")
def ai_summary(nid: int, user: dict = Depends(current_user)):
    n = must(db.get("notices", nid), "标讯")
    if not llm.enabled():
        raise HTTPException(400, "尚未配置 AI 模型，请到「系统设置 → AI 模型」配置后再用")
    if not n["detail_fetched"] and not n["url"].startswith("manual://"):
        try:
            n = runner.fetch_detail(nid)
        except Exception:  # noqa: BLE001
            pass
    prompt = (
        "请用大白话把这条招标公告讲清楚，给投标企业的老板看。只依据公告原文，不要编造。输出 JSON：\n"
        '{"one_line":"一句话说清楚买什么、多少钱、什么时候截止",'
        '"requirements":["资格要求要点（证书、业绩、人员、财务等，逐条）"],'
        '"scoring":"评分方式（综合评分/最低价等）及要点，公告没写就写「见招标文件」",'
        '"key_dates":["报名/获取文件截止","投标截止","开标"],'
        '"risks":["容易踩坑或需要特别注意的地方"],'
        '"suggestion":"值不值得投的初步建议和理由"}\n\n'
        "【公告标题】" + n["title"] + "\n【公告正文】\n" + (n["content_text"] or "")[:12000]
    )
    try:
        data = llm.chat_json(prompt, system="你是招投标顾问，说话简洁直接。", feature="notice_summary", max_tokens=1500)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"AI 解读失败：{e}") from e
    db.update("notices", nid, {"ai_summary_json": db.jdump(data), "updated_at": now_str()})
    return data


@router.post("/notices/import")
def import_notice(payload: dict = Body(...), user: dict = Depends(current_user)):
    try:
        if payload.get("url") and not payload.get("text"):
            nid = runner.import_url(payload["url"].strip())
        else:
            if not (payload.get("text") or "").strip():
                raise HTTPException(400, "请粘贴公告链接或公告正文")
            nid = runner.import_text(payload.get("title") or "", payload["text"], payload.get("url") or "")
    except (StopCrawl, RobotsDisallowed) as e:
        raise HTTPException(429, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"导入失败：{type(e).__name__} {e}（可以改用「粘贴正文」导入）") from e
    db.audit(user, "导入标讯", str(nid))
    return {"id": nid}


@router.post("/notices/{nid}/rematch")
def rematch_notice(nid: int, user: dict = Depends(current_user)):
    must(db.get("notices", nid), "标讯")
    return matching.match_notice(nid)


# ---------------------------------------------------------------- 匹配结果

@router.post("/matches/{mid}/status")
def set_match_status(mid: int, payload: dict = Body(...), user: dict = Depends(current_user)):
    m = must(db.get("matches", mid), "匹配记录")
    data = {"updated_at": now_str()}
    st = payload.get("status")
    if st:
        if st not in ("new", "fav", "ignored", "project"):
            raise HTTPException(400, "状态不正确")
        data["status"] = st
    if "assigned_to" in payload:
        data["assigned_to"] = payload["assigned_to"] or None
        if payload["assigned_to"]:
            n = db.get("notices", m["notice_id"])
            from .. import notify

            notify.add(f"{user.get('display_name') or user['username']} 指派给你一条标讯", n["title"], kind="assign", link=f"#/notices/{n['id']}", user_id=int(payload["assigned_to"]))
    db.update("matches", mid, data)
    return _match_out(db.get("matches", mid))


@router.post("/matches/{mid}/ai_check")
def ai_check_match(mid: int, user: dict = Depends(current_user)):
    m = must(db.get("matches", mid), "匹配记录")
    if not llm.enabled():
        raise HTTPException(400, "尚未配置 AI 模型")
    n = db.get("notices", m["notice_id"])
    if not n["detail_fetched"] and not n["url"].startswith("manual://"):
        try:
            runner.fetch_detail(n["id"])
        except Exception:  # noqa: BLE001
            pass
    try:
        return _match_out(matching.ai_recheck_match(mid))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"AI 资格复核失败：{e}") from e


# ---------------------------------------------------------------- 采集来源

SOURCE_FIELDS = ["name", "description", "enabled"]


@router.post("/notices/bulk_delete")
def bulk_delete_notices(payload: dict = Body(...), user: dict = Depends(require("admin", "manager"))):
    """按选中的 id 批量删除公告。"""
    raw = payload.get("ids") or []
    if not isinstance(raw, list):
        raise HTTPException(400, "ids 需要是数组")
    try:
        ids = [int(x) for x in raw]
    except (TypeError, ValueError):
        raise HTTPException(400, "ids 里有非法值")
    if not ids:
        raise HTTPException(400, "没有选中任何公告")
    if len(ids) > 5000:
        raise HTTPException(400, "一次最多删除 5000 条，请分批操作")
    return _delete_notice_ids(ids, user)


@router.post("/notices/purge")
def purge_notices(payload: dict = Body(...), user: dict = Depends(require("admin"))):
    """按当前筛选条件整批清空（只删筛选结果，不动已立项的）。"""
    f = payload.get("filter") or {}
    if not isinstance(f, dict):
        raise HTTPException(400, "filter 需要是对象")
    join, where, params = _notice_query(
        company_id=int(f.get("company_id") or 0),
        only_matched=int(f.get("only_matched") or 0),
        level=(f.get("level") or ""),
        status=(f.get("status") or ""),
        notice_type=(f.get("notice_type") or ""),
        region=(f.get("region") or ""),
        q=(f.get("q") or ""),
        date_from=(f.get("date_from") or ""),
        date_to=(f.get("date_to") or ""),
        hide_expired=int(f.get("hide_expired") or 0),
    )
    rows = db.q(f"SELECT DISTINCT n.id FROM notices n {join} WHERE {' AND '.join(where)} LIMIT 20000", params)
    return _delete_notice_ids([r["id"] for r in rows], user)


@router.get("/sources")
def list_sources(user: dict = Depends(current_user)):
    rows = db.q("SELECT * FROM sources ORDER BY id")
    for r in rows:
        r["config"] = db.jload(r.pop("config_json"), {})
        r["type_name"] = runner.SOURCE_TYPES.get(r["type"], r["type"])
        r["notice_count"] = db.scalar("SELECT COUNT(*) FROM notices WHERE source_id = ?", (r["id"],))
    return {"sources": rows, "running": runner.is_running(), "ccgp_channels": ccgp.CHANNELS, "types": runner.SOURCE_TYPES}


@router.post("/sources")
def create_source(payload: dict = Body(...), user: dict = Depends(require("admin"))):
    typ = payload.get("type") or "custom_list"
    if typ not in runner.SOURCE_TYPES:
        raise HTTPException(400, "来源类型不正确")
    data = pick(payload, SOURCE_FIELDS)
    if not data.get("name"):
        raise HTTPException(400, "请填写来源名称")
    cfg = payload.get("config") or {}
    if typ == "custom_list" and not (cfg.get("list_url") or "").startswith(("http://", "https://")):
        raise HTTPException(400, "请填写列表页地址（http/https）")
    data.update({"type": typ, "config_json": db.jdump(cfg), "enabled": 1 if data.get("enabled", 1) in (1, True, "1") else 0, "created_at": now_str()})
    sid = db.insert("sources", data)
    db.audit(user, "新增采集来源", data["name"])
    return db.get("sources", sid)


@router.put("/sources/{sid}")
def update_source(sid: int, payload: dict = Body(...), user: dict = Depends(require("admin"))):
    must(db.get("sources", sid), "来源")
    data = pick(payload, SOURCE_FIELDS)
    if "enabled" in data:
        data["enabled"] = 1 if data["enabled"] in (1, True, "1", "true") else 0
    if "config" in payload:
        data["config_json"] = db.jdump(payload["config"] or {})
    if payload.get("resume"):
        data["paused_until"] = None
    db.update("sources", sid, data)
    return {"ok": True}


@router.delete("/sources/{sid}")
def delete_source(sid: int, user: dict = Depends(require("admin"))):
    db.ex("DELETE FROM sources WHERE id = ?", (sid,))
    return {"ok": True}


@router.post("/sources/test")
def test_source(payload: dict = Body(...), user: dict = Depends(require("admin"))):
    try:
        return runner.test_custom_source(payload.get("config") or {})
    except (StopCrawl, RobotsDisallowed) as e:
        raise HTTPException(429, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"测试失败：{type(e).__name__} {e}") from e


@router.post("/sources/{sid}/test")
def test_source_now(sid: int, user: dict = Depends(require("admin", "manager"))):
    """测试连接：只发一两个请求，确认这个来源现在还能不能采到东西。"""
    src = must(db.get("sources", sid), "来源")
    return runner.test_source(src)


@router.post("/sources/{sid}/run")
def run_source_now(sid: int, user: dict = Depends(require("admin", "manager", "sales"))):
    must(db.get("sources", sid), "来源")

    def task(jid, source_id):
        return runner.run_source(source_id, trigger="manual", log_cb=lambda msg: jobs.progress(jid, 0, message=msg))

    jid = jobs.start("crawl", task, sid, user_id=user["id"], message="采集中…")
    return {"job_id": jid}


@router.post("/crawl/run_all")
def run_all_now(user: dict = Depends(require("admin", "manager", "sales"))):
    if runner.is_running():
        raise HTTPException(400, "采集正在进行中，请稍后")

    def task(jid):
        return runner.run_all("manual")

    jid = jobs.start("crawl_all", task, user_id=user["id"], message="正在采集全部来源…")
    return {"job_id": jid}


@router.get("/crawl/runs")
def crawl_runs(source_id: int = 0, limit: int = 30, user: dict = Depends(current_user)):
    if source_id:
        return db.q("SELECT r.*, s.name AS source_name FROM crawl_runs r LEFT JOIN sources s ON s.id = r.source_id WHERE r.source_id = ? ORDER BY r.id DESC LIMIT ?", (source_id, limit))
    return db.q("SELECT r.*, s.name AS source_name FROM crawl_runs r LEFT JOIN sources s ON s.id = r.source_id ORDER BY r.id DESC LIMIT ?", (limit,))
