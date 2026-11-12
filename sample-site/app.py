"""离线样本站：模拟公路施工封闭公告站点，覆盖目录迁移与跳转场景。

数据全部内置，无外部依赖，供采集 API 离线联调。
页面结构与真实公告站点同构：
- /announcements?page=N        列表页（含“下一页”同站分页；故意含一个失效链接，
                               用于验证“采集失败 -> 无法判定”的处理）
- /announcement/<id>           详情页（路段/方向/生效日期/每周封闭日/起止钟点）
- /bulletin/announcements      目录迁移后的新列表（相对分页/详情链接，无失效链接）
- /bulletin/announcement/<id>  迁移后的新详情页
- /moved/announcements         旧目录 -> 302 同站跳转新目录（站内迁移）
- /legacy/announcement/<id>    旧详情 -> 302 同站跳转新详情（详情站内迁移）
- /mixed/announcements         同一公告同时以新地址与旧跳转地址出现（按最终地址去重）
- /mirror/announcements        -> 302 跨站跳转（采集方应拒绝并记录 list 失败）
- /remote/announcements        列表正常，但详情 -> 302 跨站跳转（应记录 detail 失败）
- /loop/announcements          -> 302 自跳转（跳转链循环，应记录 list 失败）
"""
from __future__ import annotations

from datetime import date
from flask import Flask, abort, redirect, render_template_string, request

app = Flask(__name__)

LIST_TEMPLATE = """
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>施工封闭公告列表</title></head>
<body>
<h1>公路施工封闭公告</h1>
<table class="announcement-table">
  <thead><tr><th>公告标题</th><th>发布日期</th></tr></thead>
  <tbody>
  {% for item in items %}
    <tr>
      <td><a href="{{ item.href }}">{{ item.title }}</a></td>
      <td>{{ item.published }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<div class="pagination">
  {% if page > 1 %}<a rel="prev" href="{{ page_base }}?page={{ page - 1 }}">上一页</a>{% endif %}
  {% if has_next %}<a rel="next" href="{{ page_base }}?page={{ page + 1 }}">下一页</a>{% endif %}
</div>
</body></html>
"""

DETAIL_TEMPLATE = """
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{{ ann.title }}</title></head>
<body>
<article class="content">
  <h1>{{ ann.title }}</h1>
  <p>路段编号：{{ ann.road_id }}</p>
  <p>方向：{{ ann.direction }}</p>
  <p>生效日期：{{ ann.effective_text }}</p>
  <p>封闭日：{{ ann.weekday_text }}</p>
  <p>封闭时间：{{ ann.clock_text }}</p>
  <p>管制措施：施工期间该路段禁止车辆通行，请提前绕行。</p>
</article>
</body></html>
"""

ANNOUNCEMENTS = [
    {
        "id": "gg-2026-001",
        "title": "G15沈海高速K123段下行夜间施工封闭公告",
        "published": "2026-11-01",
        "road_id": "G15-K123",
        "direction": "下行",
        "effective_start": date(2026, 11, 1),
        "effective_end": date(2026, 11, 30),
        "effective_text": "2026年11月1日至2026年11月30日",
        "weekday_text": "每周一、周三",
        "clock_text": "22:00 至 次日 05:00（跨午夜，归属当日封闭）",
    },
    {
        "id": "gg-2026-002",
        "title": "G15沈海高速K123段上行白天施工封闭公告",
        "published": "2026-11-02",
        "road_id": "G15-K123",
        "direction": "上行",
        "effective_start": date(2026, 11, 5),
        "effective_end": date(2026, 11, 25),
        "effective_text": "2026年11月5日至2026年11月25日",
        "weekday_text": "每周二、周四",
        "clock_text": "09:00 至 16:30",
    },
    {
        "id": "gg-2026-003",
        "title": "G25长深高速K88段双向周末施工封闭公告",
        "published": "2026-11-03",
        "road_id": "G25-K88",
        "direction": "双向",
        "effective_start": date(2026, 10, 15),
        "effective_end": None,
        "effective_text": "2026年10月15日起长期有效",
        "weekday_text": "每周六、周日",
        "clock_text": "00:00 至 24:00（全天封闭）",
    },
]

PAGE_SIZE = 2
# 故意放在主列表里的失效链接，验证采集失败处理
BROKEN_HREF = "/announcement/gg-2026-deleted"
# 跨站跳转目标：.invalid 域名永不解析（RFC 2606），保证离线环境也不会真正访问
EXTERNAL_SITE = "http://closure-notice.other-bureau.invalid"


def _ann_item(ann: dict, href: str) -> dict:
    return {"href": href, "title": ann["title"], "published": ann["published"]}


def _main_items() -> list[dict]:
    items = [
        _ann_item(a, f"/announcement/{a['id']}") for a in ANNOUNCEMENTS
    ]
    items.insert(1, {"href": BROKEN_HREF, "title": "【已撤销】临时管制公告", "published": "2026-11-04"})
    return items


def _bulletin_items() -> list[dict]:
    # 迁移后的新目录：相对链接，须按最终页面地址解析；不含失效链接
    return [_ann_item(a, f"announcement/{a['id']}") for a in ANNOUNCEMENTS]


def _render_listing(items: list[dict], page_base: str):
    page = max(1, request.args.get("page", default=1, type=int))
    start = (page - 1) * PAGE_SIZE
    page_items = items[start : start + PAGE_SIZE]
    if not page_items and page != 1:
        abort(404)
    has_next = start + PAGE_SIZE < len(items)
    return render_template_string(
        LIST_TEMPLATE, items=page_items, page=page, has_next=has_next,
        page_base=page_base,
    )


def _render_detail(ann_id: str):
    ann = next((a for a in ANNOUNCEMENTS if a["id"] == ann_id), None)
    if ann is None:
        abort(404)
    return render_template_string(DETAIL_TEMPLATE, ann=ann)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---- 原始目录（保持不动，含一个失效链接） -------------------------------

@app.get("/announcements")
def listing():
    return _render_listing(_main_items(), "/announcements")


@app.get("/announcement/<ann_id>")
def detail(ann_id: str):
    return _render_detail(ann_id)


# ---- 站内迁移：新目录 + 旧目录 302 跳转 ---------------------------------

@app.get("/bulletin/announcements")
def bulletin_listing():
    return _render_listing(_bulletin_items(), "announcements")


@app.get("/bulletin/announcement/<ann_id>")
def bulletin_detail(ann_id: str):
    return _render_detail(ann_id)


@app.get("/moved/announcements")
def moved_listing():
    """旧目录地址：整站目录迁移，302 到同站新路径（保留查询串）。"""
    target = "/bulletin/announcements"
    if request.query_string:
        target += "?" + request.query_string.decode()
    return redirect(target, code=302)


@app.get("/legacy/announcement/<ann_id>")
def legacy_detail(ann_id: str):
    """旧详情地址：302 到同站新详情。"""
    return redirect(f"/bulletin/announcement/{ann_id}", code=302)


@app.get("/mixed/announcements")
def mixed_listing():
    """同一公告以新地址与旧跳转地址重复出现，验证按最终地址去重。"""
    ann = ANNOUNCEMENTS[0]
    items = [
        _ann_item(ann, f"/bulletin/announcement/{ann['id']}"),
        _ann_item(ann, f"/legacy/announcement/{ann['id']}"),
    ]
    return render_template_string(
        LIST_TEMPLATE, items=items, page=1, has_next=False,
        page_base="/mixed/announcements",
    )


# ---- 跨站跳转与跳转链异常 ------------------------------------------------

@app.get("/mirror/announcements")
def mirror_listing():
    """目录整体跳到别站：采集方应拒绝跟随并记录问题地址。"""
    return redirect(f"{EXTERNAL_SITE}/announcements", code=302)


@app.get("/remote/announcements")
def remote_listing():
    """列表在本站，但详情跳到别站。"""
    items = [
        {"href": "/remote/announcement/gg-9001", "title": "S1省道K5段施工封闭公告",
         "published": "2026-11-05"},
    ]
    return render_template_string(
        LIST_TEMPLATE, items=items, page=1, has_next=False,
        page_base="/remote/announcements",
    )


@app.get("/remote/announcement/<ann_id>")
def remote_detail(ann_id: str):
    return redirect(f"{EXTERNAL_SITE}/announcement/{ann_id}", code=302)


@app.get("/loop/announcements")
def loop_listing():
    """自跳转：跳转链循环。"""
    return redirect("/loop/announcements", code=302)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
