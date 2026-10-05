#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
礼品库存申领系统 · 同步服务（路线2）
- 轮询申领表：新单回填 礼品名称/当前可用，库存校验，置状态，发群通知
- 每轮重算各批次已通过合计，写回库存表「已批领用」（幂等，自愈）
- 并发超领保护：已通过单按修改时间 FIFO 占用库存，超出的自动回退「库存不足」

用法: python3 gift_sync.py [config.json] [--once]
依赖: python3 + requests
"""
import base64
import hashlib
import hmac
import json
import logging
import sys
import time
from collections import defaultdict

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gift_sync")

# ---------------- 配置 ----------------
CONFIG_PATH = next((a for a in sys.argv[1:] if not a.startswith("--")), "config.json")
ONCE = "--once" in sys.argv

with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)

DOMAIN = CFG.get("domain", "https://open.larksuite.com").rstrip("/")
APP_TOKEN = CFG["app_token"]
INV_TABLE = CFG["inv_table"]   # 库存表 table_id
REQ_TABLE = CFG["req_table"]   # 申领表 table_id
WEBHOOK_URL = CFG["webhook_url"]  # 群自定义机器人 Webhook，无需应用 IM 权限
WEBHOOK_SECRET = CFG.get("webhook_secret", "")  # 签名密钥（群机器人开了校验就必须带）
POLL_SEC = int(CFG.get("poll_interval_sec", 30))
LINK_PREFIX = CFG.get("base_link_prefix", "https://www.larksuite.com/base").rstrip("/")

# 字段名映射：Base 里改了字段名只需改 config，不用动代码
_fn = CFG.get("field_names", {})
F_INV_NAME = _fn.get("inv_name", "礼品名称")
F_INV_STOCK = _fn.get("inv_stock_in", "入库数量")
F_INV_APPROVED = _fn.get("inv_approved", "已批领用")
F_REQ_NO = _fn.get("req_no", "单号")
F_REQ_QTY = _fn.get("req_qty", "申领数量")
F_REQ_STATUS = _fn.get("req_status", "审批状态")
F_REQ_LINK = _fn.get("req_link", "关联批次")
F_REQ_NAME = _fn.get("req_name", "礼品名称")
F_REQ_AVAIL = _fn.get("req_available", "当前可用")

ST_REVIEW, ST_APPROVED, ST_SHORT = "待审批", "已通过", "库存不足"


# ---------------- 工具 ----------------
def link_ids(v):
    """关联字段值 -> [record_id]，兼容各租户返回形态（本租户实测: [{record_ids:[...],...}]）"""
    def from_dict(x):
        ids = x.get("record_ids") or x.get("link_record_ids") or []
        if x.get("record_id"):
            ids = ids + [x["record_id"]]
        return [r for r in ids if r]
    if not v:
        return []
    if isinstance(v, dict):
        return from_dict(v)
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                out += from_dict(x)
        return out
    return []


def num(v):
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return 0.0
    if isinstance(v, list):
        return num(v[0]) if v else 0.0
    return 0.0


def numval(x):
    x = float(x)
    return int(x) if x.is_integer() else round(x, 2)


def plain(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(numval(v))
    if isinstance(v, list):
        return " ".join(plain(x) for x in v)
    if isinstance(v, dict):
        return str(v.get("text") or v.get("value") or "")
    return str(v)


# ---------------- Lark 客户端 ----------------
class LarkClient:
    def __init__(self, app_id, app_secret):
        self.app_id, self.app_secret = app_id, app_secret
        self._token, self._exp = None, 0.0

    def _refresh(self):
        d = requests.post(
            f"{DOMAIN}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        ).json()
        if d.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {d.get('code')} {d.get('msg')}")
        self._token = d["tenant_access_token"]
        self._exp = time.time() + int(d.get("expire", 7200)) - 300

    def _headers(self):
        if self._token is None or time.time() >= self._exp:
            self._refresh()
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method, path, **kw):
        for i in range(2):
            resp = requests.request(method, DOMAIN + path, headers=self._headers(), timeout=20, **kw)
            d = resp.json()
            if d.get("code", 0) in (99991661, 99991663, 99991668) and i == 0:
                self._exp = 0.0  # token 失效，强刷后重试一次
                continue
            if d.get("code", 0) != 0:
                raise RuntimeError(f"{method} {path} 失败: code={d.get('code')} msg={d.get('msg')}")
            return d.get("data") or {}
        raise RuntimeError("unreachable")

    def list_records(self, table_id):
        items, page = [], None
        while True:
            params = {"page_size": 500}
            if page:
                params["page_token"] = page
            d = self._call("GET", f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records", params=params)
            items += d.get("items") or []
            if not d.get("has_more"):
                return items
            page = d.get("page_token")

    def update_record(self, table_id, record_id, fields):
        self._call("PUT", f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/{record_id}",
                   json={"fields": fields})



def notify(text):
    """经群自定义机器人 Webhook 发通知（带签名），不占用应用 IM 权限"""
    payload = {"msg_type": "text", "content": {"text": text}}
    if WEBHOOK_SECRET:
        ts = str(int(time.time()))
        sign = base64.b64encode(hmac.new(f"{ts}\n{WEBHOOK_SECRET}".encode(), digestmod=hashlib.sha256).digest())
        payload["timestamp"] = ts
        payload["sign"] = sign.decode()
    d = requests.post(WEBHOOK_URL, json=payload, timeout=15).json()
    code = d.get("code", d.get("StatusCode"))
    if code not in (0, None):
        raise RuntimeError(f"Webhook 发送失败: {d}")


# ---------------- 主逻辑 ----------------
def cycle(client):
    reqs = client.list_records(REQ_TABLE)
    invs = client.list_records(INV_TABLE)
    inv_by_id = {r["record_id"]: r for r in invs}

    def stock(rid):
        return num(inv_by_id.get(rid, {}).get("fields", {}).get(F_INV_STOCK))

    def iname(rid):
        return plain(inv_by_id.get(rid, {}).get("fields", {}).get(F_INV_NAME))

    # 1) 已通过单按修改时间 FIFO 占用库存，找出超领的
    approved = [r for r in reqs if r["fields"].get(F_REQ_STATUS) == ST_APPROVED]
    approved.sort(key=lambda r: r.get("last_modified_time") or r.get("created_time") or "0")
    used = defaultdict(float)
    overdraft = []
    for r in approved:
        links = link_ids(r["fields"].get(F_REQ_LINK))
        b = links[0] if links else None
        q = num(r["fields"].get(F_REQ_QTY))
        if b is not None and b in inv_by_id and used[b] + q <= stock(b):
            used[b] += q
        else:
            overdraft.append((r, b))

    # 2) 超领的已通过单回退（并发双批的兜底）
    for r, b in overdraft:
        no = plain(r["fields"].get(F_REQ_NO)) or r["record_id"][-6:]
        avail = stock(b) - used.get(b, 0.0) if b else 0
        client.update_record(REQ_TABLE, r["record_id"], {F_REQ_STATUS: ST_SHORT})
        notify(f"⚠️ 申领 {no} 通过时库存已被占用（剩余可用 {numval(avail)}），已自动回退为「库存不足」，请重新提交或联系审批人")
        log.info("超领回退 %s", no)

    # 3) 重算「已批领用」写回库存表（仅变化时写）
    for rid, inv in inv_by_id.items():
        cur = num(inv["fields"].get(F_INV_APPROVED))
        if abs(cur - used.get(rid, 0.0)) > 1e-9:
            client.update_record(INV_TABLE, rid, {F_INV_APPROVED: numval(used.get(rid, 0.0))})
            log.info("已批领用回写 [%s]: %s -> %s", iname(rid) or rid, numval(cur), numval(used.get(rid, 0.0)))

    # 4) 处理新单（审批状态为空 = 刚从表单进来）
    for r in reqs:
        f = r["fields"]
        if f.get(F_REQ_STATUS):
            continue
        links = link_ids(f.get(F_REQ_LINK))
        q = num(f.get(F_REQ_QTY))
        no = plain(f.get(F_REQ_NO)) or r["record_id"][-6:]
        if not links or q <= 0:
            continue  # 表单必填兜底，异常单留人工处理
        b = links[0]
        avail = stock(b) - used.get(b, 0.0)
        name = iname(b)
        if q > avail:
            client.update_record(REQ_TABLE, r["record_id"],
                                 {F_REQ_NAME: name, F_REQ_AVAIL: numval(avail), F_REQ_STATUS: ST_SHORT})
            notify(f"❌ 申领 {no}：{name} × {numval(q)} 超出可用库存（{numval(avail)}），已自动拦截")
            log.info("拦截 %s", no)
        else:
            client.update_record(REQ_TABLE, r["record_id"],
                                 {F_REQ_NAME: name, F_REQ_AVAIL: numval(avail), F_REQ_STATUS: ST_REVIEW})
            link = f"{LINK_PREFIX}/{APP_TOKEN}?table={REQ_TABLE}&record={r['record_id']}"
            notify(f"📋 新申领 {no}：{name} × {numval(q)}，当前可用 {numval(avail)}，待审批\n{link}")
            log.info("转审 %s", no)


def main():
    client = LarkClient(CFG["app_id"], CFG["app_secret"])
    log.info("启动成功，轮询间隔 %ss，Webhook 通知已配置", POLL_SEC)
    while True:
        try:
            cycle(client)
        except Exception:
            log.exception("本轮处理失败，%ss 后重试", POLL_SEC)
        if ONCE:
            break
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
