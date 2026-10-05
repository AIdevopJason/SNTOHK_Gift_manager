#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生产环境回归测试：对线上 Base 造数→驱动→断言→清理。
只碰自建的「回归测试礼品-可删」批次；开始前快照全部既有记录，结束后比对确保未动。
通知会真实发到群里（约6条）。
用法: python3 test_regression.py [config.json]
"""
import json
import sys
import time

import requests

CONFIG_PATH = next((a for a in sys.argv[1:] if not a.startswith("--")), "config.json")
with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)
DOM = CFG["domain"]
APP, INV, REQ = CFG["app_token"], CFG["inv_table"], CFG["req_table"]

results = []


def check(stage, ok, detail=""):
    results.append((stage, ok, detail))
    print(f"{'✅' if ok else '❌'} {stage} {detail}", flush=True)


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

    def call(self, method, path, **kw):
        d = requests.request(method, DOM + path, headers=self._h(), timeout=20, **kw).json()
        assert d.get("code") == 0, f"{method} {path}: {d.get('code')} {d.get('msg')}"
        return d.get("data") or {}

    def records(self, tid):
        out, page = [], None
        while True:
            p = {"page_size": 500} | ({"page_token": page} if page else {})
            d = self.call("GET", f"/open-apis/bitable/v1/apps/{APP}/tables/{tid}/records", params=p)
            out += d.get("items") or []
            if not d.get("has_more"):
                return out
            page = d["page_token"]

    def create(self, tid, fields):
        return self.call("POST", f"/open-apis/bitable/v1/apps/{APP}/tables/{tid}/records",
                         json={"fields": fields})["record"]["record_id"]

    def update(self, tid, rid, fields):
        self.call("PUT", f"/open-apis/bitable/v1/apps/{APP}/tables/{tid}/records/{rid}", json={"fields": fields})

    def delete(self, tid, rid):
        self.call("DELETE", f"/open-apis/bitable/v1/apps/{APP}/tables/{tid}/records/{rid}")


def link_ids(v):
    ids = []
    for x in (v or []):
        if isinstance(x, str):
            ids.append(x)
        elif isinstance(x, dict):
            ids += x.get("record_ids") or x.get("link_record_ids") or []
    return ids


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def wait_for(lk, desc, fn, timeout=150):
    """等服务器轮询生效：fn(reqs, invs) -> True/False"""
    deadline = time.time() + timeout
    time.sleep(15)
    while time.time() < deadline:
        reqs, invs = lk.records(REQ), lk.records(INV)
        if fn(reqs, invs):
            return reqs, invs
        time.sleep(10)
    reqs, invs = lk.records(REQ), lk.records(INV)
    return reqs, invs  # 最后一次取数供断言失败时展示


def req_of(reqs, rid):
    return next((r["fields"] for r in reqs if r["record_id"] == rid), {})


def inv_of(invs, rid):
    return next((r["fields"] for r in invs if r["record_id"] == rid), {})


def main():
    lk = Lark()
    # 快照既有记录（结束前必须原样）
    base_inv = {r["record_id"]: json.dumps(r["fields"], sort_keys=True, ensure_ascii=False) for r in lk.records(INV)}
    base_req_ids = {r["record_id"] for r in lk.records(REQ)}
    print(f"基线：库存 {len(base_inv)} 行，申领 {len(base_req_ids)} 行（不受测试影响）", flush=True)

    # 建测试批次
    batch = lk.create(INV, {"礼品名称": "回归测试礼品-可删", "品质": "B", "生产日期": 1760000000000,
                            "入库数量": 10})
    print(f"测试批次 {batch[:10]}… 入库10", flush=True)
    created_req, created_inv = [], [batch]

    try:
        # A 转审+回填
        ra = lk.create(REQ, {"关联批次": [batch], "申领数量": 3, "用途": "其他"})
        created_req.append(ra)
        _, invs = wait_for(lk, "A", lambda rq, iv: req_of(rq, ra).get("审批状态") == "待审批")
        f = req_of(lk.records(REQ), ra)
        check("A 新单转审+回填", f.get("审批状态") == "待审批" and f.get("礼品名称") == "回归测试礼品-可删"
              and num(f.get("当前可用")) == 10, f"状态={f.get('审批状态')} 名称={f.get('礼品名称')} 可用={f.get('当前可用')}")

        # B 通过→扣减
        lk.update(REQ, ra, {"审批状态": "已通过"})
        wait_for(lk, "B", lambda rq, iv: num(inv_of(iv, batch).get("已批领用")) == 3)
        g = inv_of(lk.records(INV), batch)
        check("B 通过扣减", num(g.get("已批领用")) == 3 and num(g.get("可用数量")) == 7,
              f"已批领用={g.get('已批领用')} 可用数量={g.get('可用数量')}")

        # C 拒绝不扣
        rc = lk.create(REQ, {"关联批次": [batch], "申领数量": 2, "用途": "其他"})
        created_req.append(rc)
        wait_for(lk, "C1", lambda rq, iv: req_of(rq, rc).get("审批状态") == "待审批")
        lk.update(REQ, rc, {"审批状态": "已拒绝"})
        time.sleep(35)  # 给服务器至少一个完整周期
        g = inv_of(lk.records(INV), batch)
        check("C 拒绝不扣", num(g.get("已批领用")) == 3, f"已批领用={g.get('已批领用')}")

        # D 超量拦截
        rd = lk.create(REQ, {"关联批次": [batch], "申领数量": 8, "用途": "其他"})
        created_req.append(rd)
        wait_for(lk, "D", lambda rq, iv: req_of(rq, rd).get("审批状态") == "库存不足")
        f = req_of(lk.records(REQ), rd)
        check("D 超量拦截", f.get("审批状态") == "库存不足", f"状态={f.get('审批状态')}")

        # E 并发超领回退（可用7，两单各6）
        r1 = lk.create(REQ, {"关联批次": [batch], "申领数量": 6, "用途": "其他"})
        r2 = lk.create(REQ, {"关联批次": [batch], "申领数量": 6, "用途": "其他"})
        created_req += [r1, r2]
        wait_for(lk, "E1", lambda rq, iv: req_of(rq, r1).get("审批状态") == "待审批"
                 and req_of(rq, r2).get("审批状态") == "待审批")
        lk.update(REQ, r1, {"审批状态": "已通过"})
        lk.update(REQ, r2, {"审批状态": "已通过"})
        wait_for(lk, "E2", lambda rq, iv: num(inv_of(iv, batch).get("已批领用")) == 9
                 and {req_of(rq, r1).get("审批状态"), req_of(rq, r2).get("审批状态")} == {"已通过", "库存不足"})
        f1, f2 = req_of(lk.records(REQ), r1), req_of(lk.records(REQ), r2)
        g = inv_of(lk.records(INV), batch)
        check("E 并发超领回退", num(g.get("已批领用")) == 9
              and {f1.get("审批状态"), f2.get("审批状态")} == {"已通过", "库存不足"},
              f"单1={f1.get('审批状态')} 单2={f2.get('审批状态')} 已批领用={g.get('已批领用')}")

        # F 自愈：删已通过单（B 的3）→ 9→6
        lk.delete(REQ, ra)
        created_req.remove(ra)
        wait_for(lk, "F", lambda rq, iv: num(inv_of(iv, batch).get("已批领用")) == 6)
        g = inv_of(lk.records(INV), batch)
        check("F 删单自愈", num(g.get("已批领用")) == 6, f"已批领用={g.get('已批领用')}")
    finally:
        # 清理：测试申领单 + 测试批次
        for rid in created_req:
            try:
                lk.delete(REQ, rid)
            except AssertionError as e:
                print(f"清理申领单失败 {rid}: {e}")
        for rid in created_inv:
            try:
                lk.delete(INV, rid)
            except AssertionError as e:
                print(f"清理批次失败 {rid}: {e}")
        time.sleep(35)
        # 终验：既有库存记录与基线完全一致；申领表无测试残留（只剩基线单）
        now_inv = {r["record_id"]: json.dumps(r["fields"], sort_keys=True, ensure_ascii=False)
                   for r in lk.records(INV)}
        now_req_ids = {r["record_id"] for r in lk.records(REQ)}
        diff = {k for k in base_inv if now_inv.get(k) != base_inv[k]}
        check("G 清理+基线比对", not diff and created_inv[0] not in now_inv and not (now_req_ids - base_req_ids),
              f"库存差异={list(diff)[:3]} 申领残留={list(now_req_ids - base_req_ids)[:3]}")

    ok = sum(1 for _, o, _ in results if o)
    print(f"\n===== 回归结果 {ok}/{len(results)} =====", flush=True)
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
