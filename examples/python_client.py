"""冷链追责服务 Python 调用示例（仅依赖 httpx）。

运行：python examples/python_client.py
默认地址 http://127.0.0.1:8000，可用环境变量 BASE_URL 覆盖。
"""
import os

import httpx

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")
CARGO = "COLD-DEMO-2"


def show(title: str, resp: httpx.Response) -> None:
    print(f"\n== {title} [{resp.status_code}] ==")
    print(resp.text)


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=10) as api:
        show(
            "创建货物",
            api.post(
                "/cargo",
                json={
                    "cargo_id": CARGO,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": "4.0",
                    "initial_holder": "北京冷库",
                },
            ),
        )

        # 测温：温度高于上限 -> 开启越温区间
        show(
            "超限测温",
            api.post(
                "/events",
                json={
                    "event_id": "p-t1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T10:00:00+08:00",
                    "temperature": "8.2",
                },
            ),
        )

        # 交接：必须带交出方与接收方；交出方须为当时保管方
        show(
            "合法交接 北京->上海",
            api.post(
                "/events",
                json={
                    "event_id": "p-h1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T12:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "上海冷库",
                },
            ),
        )

        # 错误交出方 -> 422，整体拒绝
        show(
            "非法交接（交出方不是保管方，拒绝）",
            api.post(
                "/events",
                json={
                    "event_id": "p-bad",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T13:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "广州冷库",
                },
            ),
        )

        # 不超限测温 -> 闭合区间
        show(
            "恢复测温（闭合区间）",
            api.post(
                "/events",
                json={
                    "event_id": "p-t2",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T15:00:00+08:00",
                    "temperature": "3.0",
                },
            ),
        )

        show("追责报告", api.get(f"/cargo/{CARGO}/report"))
        show("事件列表（按时间）", api.get(f"/cargo/{CARGO}/events"))

        # ----- 责任确认（仅针对已闭合越温区间）-----
        resp = api.get(f"/cargo/{CARGO}/responsibilities")
        show("责任确认：已闭合区间列表", resp)
        intervals = resp.json().get("intervals", [])
        if not intervals:
            print("（没有已闭合越温区间，跳过责任确认演示）")
            return

        interval = intervals[0]
        iid = interval["interval_id"]       # 区间标识 = 开启测温事件ID
        snap = interval["snapshot_id"]      # 当前快照标识（区间起止 + 责任片段）
        print(f"区间标识={iid} 结束测温事件={interval['end_event_id']} "
              f"快照={snap} 初始汇总={interval['summary']}")

        # 起始保管方确认担责
        show(
            "北京冷库 确认",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "北京冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-1",
                },
            ),
        )

        # 异议必须带理由；忘记理由 -> 422 dispute_reason_required
        show(
            "上海冷库 异议但未带理由（拒绝示例）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "dispute",
                    "stance_id": "py-stance-bad",
                },
            ),
        )

        # 合法异议
        show(
            "上海冷库 异议（带理由）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "dispute",
                    "reason": "温控参数由交出方远程设定，接收方未实际接管",
                    "stance_id": "py-stance-2",
                },
            ),
        )

        show(
            "区间责任确认视图（此时汇总 has_dispute）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}"),
        )

        # 同编号同内容重传 -> 200 replayed，返回原记录
        show(
            "表态幂等重传",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "北京冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-1",
                },
            ),
        )

        # 改口：新编号，历次表态保留
        show(
            "上海冷库 改口为确认（新编号）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-3",
                },
            ),
        )
        show(
            "表态历史（历次表态，含 superseded/is_current 标记）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}/stances"),
        )
        show(
            "最终区间视图（汇总 all_confirmed）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}"),
        )

        # ==================== 车次 / 装卸时段 / 车载测温 ====================
        trip = "TRUCK-PY-1"
        cargo_a = "COLD-CAR-A"
        cargo_b = "COLD-CAR-B"
        for cid, limit in ((cargo_a, "4.0"), (cargo_b, "8.0")):
            api.post(
                "/cargo",
                json={
                    "cargo_id": cid,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": limit,
                    "initial_holder": "北京冷库",
                },
            )

        show(
            "登记车次",
            api.post("/trips", json={
                "trip_id": trip, "vehicle_no": "京B-冷888", "note": "京沪班"}),
        )

        # 批量登记装卸时段（原子：左闭右开、同货物不得跨车次重叠，否则整批拒绝）
        show(
            "批量登记装卸时段",
            api.post(
                f"/trips/{trip}/manifest",
                json={"items": [
                    {"cargo_id": cargo_a,
                     "loaded_at": "2026-09-30T09:00:00+08:00",
                     "unloaded_at": "2026-09-30T18:00:00+08:00"},
                    {"cargo_id": cargo_b,
                     "loaded_at": "2026-09-30T09:00:00+08:00",
                     "unloaded_at": "2026-09-30T18:00:00+08:00"},
                ]},
            ),
        )

        # 非法时段（起点不早于终点）-> 422，整批不留记录
        show(
            "非法装卸时段（整体拒绝）",
            api.post(
                f"/trips/{trip}/manifest",
                json={"items": [
                    {"cargo_id": cargo_a,
                     "loaded_at": "2026-09-30T19:00:00+08:00",
                     "unloaded_at": "2026-09-30T19:00:00+08:00"},
                ]},
            ),
        )

        # 车载 7℃：对上限 4℃ 的 A 越温，对上限 8℃ 的 B 不越温
        show(
            "车载读数 7℃（回执含受影响货物）",
            api.post(
                "/vehicle-readings",
                json={"reading_id": "py-vr1", "trip_id": trip,
                      "read_at": "2026-09-30T10:00:00+08:00",
                      "temperature": "7.0"},
            ),
        )
        # 同编号同内容重传 -> 200 replayed
        show(
            "车载读数幂等重传",
            api.post(
                "/vehicle-readings",
                json={"reading_id": "py-vr1", "trip_id": trip,
                      "read_at": "2026-09-30T10:00:00+08:00",
                      "temperature": "7.0"},
            ),
        )

        # 11:00：货物 A 有自有测温 3.1℃，车载 7℃ 被取代（原始仍可查）
        api.post(
            "/events",
            json={"event_id": "py-ca-own1", "cargo_id": cargo_a,
                  "occurred_at": "2026-09-30T11:00:00+08:00",
                  "temperature": "3.1"},
        )
        show(
            "同刻车载读数（对 A 被自有测温取代）",
            api.post(
                "/vehicle-readings",
                json={"reading_id": "py-vr2", "trip_id": trip,
                      "read_at": "2026-09-30T11:00:00+08:00",
                      "temperature": "7.0"},
            ),
        )

        # 交接仍只由货物自有事件改变保管方
        for cid, hid in ((cargo_a, "py-ca-h1"), (cargo_b, "py-cb-h1")):
            api.post(
                "/events",
                json={"event_id": hid, "cargo_id": cid,
                      "occurred_at": "2026-09-30T12:00:00+08:00",
                      "giver": "北京冷库", "receiver": "上海冷库"},
            )

        # 车载 3℃ 闭合 A 的区间
        show(
            "车载 3℃ 闭合区间",
            api.post(
                "/vehicle-readings",
                json={"reading_id": "py-vr3", "trip_id": trip,
                      "read_at": "2026-09-30T14:00:00+08:00",
                      "temperature": "3.0"},
            ),
        )

        show("车次详情（读数 + 各货物适用性 / 采用 / 越温）", api.get(f"/trips/{trip}"))
        show("货物 A 合并读数时间线（vr2 adopted=false）",
             api.get(f"/cargo/{cargo_a}/timeline"))
        show("货物 A 追责报告（区间 veh:py-vr1，车载开启/闭合）",
             api.get(f"/cargo/{cargo_a}/report"))
        show("货物 B 追责报告（7℃ 不超其 8℃ 上限，无越温）",
             api.get(f"/cargo/{cargo_b}/report"))

        # 对车载读数开启的区间表态：区间标识 veh:<读数ID>
        ivs = api.get(f"/cargo/{cargo_a}/responsibilities").json().get("intervals", [])
        if ivs:
            viid, vsnap = ivs[0]["interval_id"], ivs[0]["snapshot_id"]
            print(f"车载区间标识={viid} 快照={vsnap}")
            show(
                "北京冷库对车载区间确认",
                api.post(
                    f"/cargo/{cargo_a}/responsibilities/{viid}/stances",
                    json={"snapshot_id": vsnap,
                          "responsible_party": "北京冷库",
                          "stance": "confirm",
                          "stance_id": "py-stance-ca-1"},
                ),
            )
            show("车载区间表态历史",
                 api.get(f"/cargo/{cargo_a}/responsibilities/{viid}/stances"))

        # ==================== 质控：同一车次同时越温查询 ====================
        # 再挂一票上限 4℃ 的货物 Q：A、Q 上限 4℃，B 上限 8℃。
        # 10:00 车载 7℃：A、Q 越温（B 不越）；11:00 A 有自有 3.1℃ 测温先闭合
        # （同刻车载 7℃ 对 A 被取代），Q 仍越温；14:00 车载 3℃ 闭合 Q。
        cargo_q = "COLD-CAR-Q"
        show(
            "创建质控演示货物 Q（上限 4℃）",
            api.post(
                "/cargo",
                json={
                    "cargo_id": cargo_q,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": "4.0",
                    "initial_holder": "北京冷库",
                },
            ),
        )
        show(
            "Q 挂到同一车次",
            api.post(
                f"/trips/{trip}/manifest",
                json={"items": [
                    {"cargo_id": cargo_q,
                     "loaded_at": "2026-09-30T09:00:00+08:00",
                     "unloaded_at": "2026-09-30T18:00:00+08:00"},
                ]},
            ),
        )

        show(
            "同时越温时段（默认门槛 2）",
            api.get(
                f"/trips/{trip}/simultaneous-excursions",
                params={
                    "start_at": "2026-09-30T00:00:00+08:00",
                    "end_at": "2026-10-01T00:00:00+08:00",
                },
            ),
        )
        show(
            "门槛 3（B 上限 8℃ 全程不越温 -> 空结果）",
            api.get(
                f"/trips/{trip}/simultaneous-excursions",
                params={
                    "start_at": "2026-09-30T00:00:00+08:00",
                    "end_at": "2026-10-01T00:00:00+08:00",
                    "min_cargo": 3,
                },
            ),
        )
        show(
            "查询窗裁剪（09:30-10:30）",
            api.get(
                f"/trips/{trip}/simultaneous-excursions",
                params={
                    "start_at": "2026-09-30T09:30:00+08:00",
                    "end_at": "2026-09-30T10:30:00+08:00",
                },
            ),
        )
        show(
            "非法查询窗（终点不晚于起点，422）",
            api.get(
                f"/trips/{trip}/simultaneous-excursions",
                params={
                    "start_at": "2026-10-01T00:00:00+08:00",
                    "end_at": "2026-09-30T00:00:00+08:00",
                },
            ),
        )


if __name__ == "__main__":
    main()
