#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动建表：在 config.json 指定的 Base 里创建「礼品库存」「礼品申领」两张表及字段。
公式（批次号/可用数量）与按钮（通过/拒绝）字段该租户 API 不支持创建，见结束提示，UI 手动加。
用法: python3 setup_tables.py [config.json]
幂等：已存在的表/字段跳过。字段 schema 用国际版新键名 type（field_type 会报 99992402）。
"""
import json
import sys
import time

import requests

CONFIG_PATH = next((a for a in sys.argv[1:] if not a.startswith("--")), "config.json")
with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)

DOMAIN = CFG.get("domain", "https://open.larksuite.com").rstrip("/")
APP_TOKEN = CFG["app_token"]

INV_FIELDS = [
    {"field_name": "礼品名称", "type": 1},
    {"field_name": "品质", "type": 3, "property": {"options": [{"name": s} for s in ("S", "A", "B", "C")]}},
    {"field_name": "生产日期", "type": 5},
    {"field_name": "图片", "type": 17},
    {"field_name": "入库数量", "type": 2, "property": {"formatter": "0"}},
    {"field_name": "已批领用", "type": 2, "property": {"formatter": "0"}},
]

REQ_FIELDS = [
    {"field_name": "单号", "type": 1005},
    {"field_name": "申请人", "type": 1003},
    {"field_name": "用途", "type": 3,
     "property": {"options": [{"name": s} for s in ("客户拜访", "展会", "内部活动", "其他")]}},
    {"field_name": "申领数量", "type": 2, "property": {"formatter": "0"}},
    {"field_name": "审批状态", "type": 3,
     "property": {"options": [{"name": s} for s in ("待审批", "已通过", "已拒绝", "库存不足")]}},
    {"field_name": "申领时间", "type": 1001},
    {"field_name": "礼品名称", "type": 1},
    {"field_name": "当前可用", "type": 2, "property": {"formatter": "0"}},
]

MANUAL_NOTES = """
还需在 UI 手动完成（API 不支持，共 4 个字段 + 配置项，见 README Step 3-4）：
  库存表 · 批次号  (公式)  = 礼品名称&"-"&品质&"-"&DATESTR(生产日期)
  库存表 · 可用数量(公式)  = 入库数量-已批领用
  申领表 · 通过 / 拒绝 (按钮字段 ×2)
  之后：2 条按钮自动化、表单视图、画廊/筛选视图
"""


class Lark:
    def __init__(self, app_id, app_secret):
        self.app_id, self.app_secret = app_id, app_secret
        self._token, self._exp = None, 0.0

    def _headers(self):
        if self._token is None or time.time() >= self._exp:
            d = requests.post(f"{DOMAIN}/open-apis/auth/v3/tenant_access_token/internal",
                              json={"app_id": self.app_id, "app_secret": self.app_secret},
                              timeout=15).json()
            if d.get("code") != 0:
                raise SystemExit(f"获取 token 失败: {d.get('code')} {d.get('msg')}")
            self._token = d["tenant_access_token"]
            self._exp = time.time() + int(d.get("expire", 7200)) - 300
        return {"Authorization": f"Bearer {self._token}"}

    def call(self, method, path, **kw):
        d = requests.request(method, DOMAIN + path, headers=self._headers(), timeout=20, **kw).json()
        return d.get("code") in (0,), d

    def list_tables(self):
        ok, d = self.call("GET", f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables",
                          params={"page_size": 100})
        if not ok:
            raise SystemExit(f"列出数据表失败: {d.get('code')} {d.get('msg')}（检查 app_token / 应用是否已加为 Base 协作者）")
        return {t["name"]: t["table_id"] for t in (d.get("data", {}).get("items") or [])}

    def list_field_names(self, table_id):
        ok, d = self.call("GET", f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields",
                          params={"page_size": 200})
        return {f["field_name"] for f in (d.get("data", {}).get("items") or [])} if ok else set()

    def add_fields_one_by_one(self, table_id, fields):
        for spec in fields:
            ok, d = self.call("POST", f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields", json=spec)
            tag = "+" if ok else f"!(code={d.get('code')} {d.get('msg')})"
            print(f"  {tag} {spec['field_name']}")

    def ensure_table(self, name, fields, link_spec=None):
        tables = self.list_tables()
        if name in tables:
            tid = tables[name]
            print(f"[{name}] 已存在，补缺字段")
        else:
            ok, d = self.call("POST", f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables",
                              json={"table": {"name": name, "fields": fields}})
            if ok:
                tid = d["data"]["table_id"]
                print(f"[{name}] 已创建（含全部字段）")
                if link_spec:
                    ok2, d2 = self.call("POST", f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{tid}/fields",
                                        json=link_spec)
                    print(f"  {'+' if ok2 else '!'} {link_spec['field_name']}")
                return tid
            print(f"[{name}] 批量创建失败({d.get('code')})，退化为空表+逐字段")
            ok, d = self.call("POST", f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables",
                              json={"table": {"name": name}})
            if not ok:
                raise SystemExit(f"建表失败 [{name}]: {d.get('code')} {d.get('msg')}")
            tid = d["data"]["table_id"]
        have = self.list_field_names(tid)
        missing = [f for f in fields if f["field_name"] not in have]
        if link_spec and link_spec["field_name"] not in have:
            missing.append(link_spec)
        self.add_fields_one_by_one(tid, missing)
        return tid


def main():
    lk = Lark(CFG["app_id"], CFG["app_secret"])
    inv_id = lk.ensure_table("礼品库存", INV_FIELDS)
    req_id = lk.ensure_table("礼品申领", REQ_FIELDS,
                             link_spec={"field_name": "关联批次", "type": 21,
                                        "property": {"table_id": inv_id, "multiple": False}})
    print(f"\n完成。inv_table: {inv_id}\n      req_table: {req_id}")
    print(MANUAL_NOTES)
    print('填回 config.json: "app_token" 保持不变，inv_table / req_table 用上面两个值')


if __name__ == "__main__":
    main()
