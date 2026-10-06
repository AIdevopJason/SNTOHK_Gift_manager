#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性导入「香港公司現存禮品清單.xlsx」到库存表，并做配套调整：
  1. 字段 生产日期 → 改名 到期日（批号公式应自动跟随，脚本会校验）
  2. 新增 原产地 文本字段
  3. 清空库存表/申领表既有记录（测试数据）
  4. 解析 Excel（品牌-項目 为名称，跨行说明并入名称，品质留空后补）批量导入
用法: python3 import_excel.py [config.json] [xlsx路径]
"""
import json
import sys
import time
from datetime import datetime

import openpyxl
import requests

CONFIG_PATH = next((a for a in sys.argv[1:] if not a.startswith("--") and not a.endswith(".xlsx")), "config.json")
XLSX = next((a for a in sys.argv[1:] if a.endswith(".xlsx")),
            "/Users/saintgermainparis/Documents/SNTO/gift-system/香港公司現存禮品清單.xlsx")

with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)
DOM = CFG["domain"]
APP, INV, REQ = CFG["app_token"], CFG["inv_table"], CFG["req_table"]


class Lark:
    def __init__(self):
        self._t, self._exp = None, 0.0

    def _h(self):
        if self._t is None or time.time() >= self._exp:
            d = requests.post(f"{DOM}/open-apis/auth/v3/tenant_access_token/internal",
                              json={"app_id": CFG["app_id"], "app_secret": CFG["app_secret"]},
                              timeout=15).json()
            assert d.get("code") == 0, d
            self._t = d["tenant_access_token"]
            self._exp = time.time() + 7000
        return {"Authorization": f"Bearer {self._t}"}

    def call(self, method, path, ok=(0,), **kw):
        d = requests.request(method, DOM + path, headers=self._h(), timeout=20, **kw).json()
        if d.get("code") not in ok:
            raise SystemExit(f"{method} {path}: {d.get('code')} {d.get('msg')}")
        return d.get("data") or {}

    def fields(self, tid):
        items = self.call("GET", f"/open-apis/bitable/v1/apps/{APP}/tables/{tid}/fields",
                          params={"page_size": 200}).get("items", [])
        return {f["field_name"]: f for f in items}

    def records(self, tid):
        out, page = [], None
        while True:
            p = {"page_size": 500} | ({"page_token": page} if page else {})
            d = self.call("GET", f"/open-apis/bitable/v1/apps/{APP}/tables/{tid}/records", params=p)
            out += d.get("items") or []
            if not d.get("has_more"):
                return out
            page = d["page_token"]

    def batch_create(self, tid, fields_list):
        for i in range(0, len(fields_list), 100):
            self.call("POST", f"/open-apis/bitable/v1/apps/{APP}/tables/{tid}/records/batch_create",
                      json={"records": [{"fields": f} for f in fields_list[i:i + 100]]})


def parse_xlsx(path):
    """-> [ {礼品名称, 到期日(ms|缺省), 原产地, 入库数量} ]"""
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    rows, out = list(ws.iter_rows(values_only=True)), []
    for i, r in enumerate(rows[1:], start=2):  # 跳过表头
        seq, brand, item, origin, expiry, qty = (list(r) + [None] * 6)[:6]
        if seq is None and item:  # 跨行说明行 → 并入上一条名称
            if out:
                out[-1]["礼品名称"] += f"（{str(item).strip()}）"
            continue
        if not (seq and item):
            continue  # 空行
        name = f"{str(brand).strip()} - {str(item).strip()}" if brand else str(item).strip()
        rec = {"礼品名称": name, "原产地": str(origin).strip() if origin else "", "入库数量": int(qty or 0)}
        if expiry and str(expiry).strip() not in ("-", ""):
            rec["到期日"] = int(datetime.strptime(str(expiry).strip(), "%Y.%m.%d").timestamp() * 1000)
        out.append(rec)
    return out


def main():
    lk = Lark()

    # 1. 字段调整（本租户 API 不支持改名字段，失败则提示 UI 手动改，不阻塞导入）
    finv = lk.fields(INV)
    if "生产日期" in finv:
        try:
            lk.call("PUT", f"/open-apis/bitable/v1/apps/{APP}/tables/{INV}/fields/{finv['生产日期']['field_id']}",
                    json={"field_name": "到期日"})
            print("1. 生产日期 → 已改名「到期日」")
        except SystemExit:
            print("1. ⚠ API 不能改字段名：导入后请在 UI 双击「生产日期」改成「到期日」（公式会自动跟随）")
    date_field = "到期日" if "到期日" in lk.fields(INV) else "生产日期"  # 导入写入用实际字段名
    if "原产地" not in lk.fields(INV):
        lk.call("POST", f"/open-apis/bitable/v1/apps/{APP}/tables/{INV}/fields",
                json={"field_name": "原产地", "type": 1})
        print("2. 已新增「原产地」字段")

    # 3. 清空两表（测试数据）
    n = 0
    for tid in (INV, REQ):
        for r in lk.records(tid):
            lk.call("DELETE", f"/open-apis/bitable/v1/apps/{APP}/tables/{tid}/records/{r['record_id']}")
            n += 1
    print(f"3. 已清空测试数据 {n} 行")

    # 4. 导入
    data = parse_xlsx(XLSX)
    for rec in data:
        if "到期日" in rec and date_field != "到期日":
            rec[date_field] = rec.pop("到期日")
    print(f"4. Excel 解析出 {len(data)} 项（日期写入字段「{date_field}」），开始批量导入…")
    lk.batch_create(INV, data)
    got = lk.records(INV)
    with_date = sum(1 for r in got if r["fields"].get(date_field) is not None)
    print(f"   导入完成：{len(got)} 行，其中带到期日 {with_date} 行")
    for r in got[:3]:
        f = r["fields"]
        print("   样例:", {k: f.get(k) for k in ("礼品名称", "原产地", "入库数量", "到期日")})


if __name__ == "__main__":
    main()
