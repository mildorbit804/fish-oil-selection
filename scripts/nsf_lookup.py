#!/usr/bin/env python3
"""查询 NSF 公开目录（info.nsf.org 膳食补充剂列表）里有没有某个厂商或产品。

只回答「NSF 目录里按这个名字查到了什么」。查不到不等于没通过检测；
网页打不开或页面结构变了，一律报错退出，不当成「查无」。

用法示例：
  python3 scripts/nsf_lookup.py --company "nordic naturals"
  python3 scripts/nsf_lookup.py --company nordic --product omega --type finished --json

退出码：0 查到条目；3 目录明确返回无匹配；2 参数错误、网络失败或页面无法解析。
"""
import argparse
import datetime
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://info.nsf.org/Certified/Dietary/Listings.asp"
EXIT_FOUND, EXIT_ERROR, EXIT_NONE = 0, 2, 3
KINDS = {"all": "", "finished": "FP", "raw": "RI"}
KIND_LABEL = {"all": "全部", "finished": "成品", "raw": "原料"}
NO_MATCH = "No Matching Products Found"

NOTE_FOUND = (
    "目录登记的是具体厂商、产品名和型号，需与手上这一瓶的品牌、型号、版本逐项对照；"
    "同厂商下有别的产品，不代表这一瓶也在内。"
)
NOTE_NONE = (
    "这只说明 NSF 目录里没有以这个名称登记的条目，不代表产品没通过检测或不合格；"
    "也可能登记在别的厂商名或产品名下。"
)

TOKEN = re.compile(
    r'(?P<standard><div align="center"><strong><font size="\+2">(?P<std>.*?)</font>)'
    r"|(?P<company><font size='\+2'>(?P<co>.*?)</font>)"
    r'|(?P<facility><strong>Facility\s*:\s*</strong>(?P<fac>.*?)</font>)'
    r'|(?P<section><font size="3"><strong>(?P<sec>[^<]*)</font>)'
    r'|(?P<category><td colspan="4"><font size="2"><strong>(?P<cat>.*?)</strong>)'
    r'|(?P<row><tr>\s*(?P<cells>(?:<td[^>]*valign="top"[^>]*>.*?</td>\s*){4})</tr>)',
    re.S,
)
CELL = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
COUNTS = re.compile(
    r"Number of matching Manufacturers is (\d+)\s*Number of matching Products is (\d+)"
)


def clean(fragment):
    text = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def validate_name(label, value):
    if value is None:
        return None
    if len(re.findall(r"[A-Za-z0-9]", value)) < 3:
        raise ValueError(f"{label}需至少 3 个英文字母或数字（NSF 目录用英文名检索）：{value!r}")
    return value.strip()


def build_url(company, product, kind):
    params = {"StandardExt": KINDS[kind], "TradeName": product or "", "CompanyName": company or ""}
    return BASE + "?" + urllib.parse.urlencode(params)


def fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fish-oil-selection nsf_lookup)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse(page):
    """返回 (无匹配?, 条目列表, 页面自报的厂商数/产品数)。"""
    text = clean(page)
    counts = COUNTS.search(text)
    totals = {"manufacturers": int(counts.group(1)), "products": int(counts.group(2))} if counts else None
    if NO_MATCH in text:
        return True, [], totals
    state = {"standard": "", "company": "", "facility": "", "section": "", "category": ""}
    entries = []
    for m in TOKEN.finditer(page):
        if m.group("standard"):
            state["standard"] = clean(m.group("std"))
        elif m.group("company"):
            state.update(company=clean(m.group("co")), facility="", section="", category="")
        elif m.group("facility"):
            state.update(facility=clean(m.group("fac")), section="", category="")
        elif m.group("section"):
            state.update(section=clean(m.group("sec")), category="")
        elif m.group("category"):
            state["category"] = clean(m.group("cat"))
        elif m.group("row"):
            cells = [clean(c) for c in CELL.findall(m.group("cells"))]
            entries.append(dict(state, trade_name=cells[0], product_id=cells[1],
                                form=cells[2], daily_serving=cells[3]))
    return False, entries, totals


def render(info):
    lines = [
        f"NSF 目录查询（info.nsf.org，查询日期 {info['queried_on']}）",
        "条件：" + "，".join(
            [f"厂商含「{info['company']}」" if info["company"] else "厂商不限",
             f"产品含「{info['product']}」" if info["product"] else "产品不限",
             f"类型：{info['kind']}"]),
    ]
    if info["no_match"]:
        lines += ["结果：无匹配条目（目录明确返回 No Matching Products Found）。", NOTE_NONE]
    else:
        shown = info["entries"][: info["max"]]
        total = info["totals"]["products"] if info["totals"] else len(info["entries"])
        lines.append(f"结果：页面共 {total} 个产品，下面显示 {len(shown)} 个。")
        last = None
        for e in shown:
            head = (e["standard"], e["company"], e["facility"], e["section"])
            if head != last:
                lines.append(f"\n[{e['standard']}] {e['company']}｜工厂：{e['facility'] or '未列'}｜{e['section']}")
                last = head
            lines.append(f"  - {e['trade_name']}｜产品 ID：{e['product_id']}｜{e['form']}｜每日份量：{e['daily_serving']}"
                         + (f"｜{e['category']}" if e["category"] else ""))
        if len(info["entries"]) > len(shown):
            lines.append(f"\n还有 {len(info['entries']) - len(shown)} 个未显示，请缩小条件或加大 --max。")
        lines += ["", NOTE_FOUND]
    lines.append(f"核对链接：{info['url']}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="查询 NSF 公开目录里的膳食补充剂登记")
    ap.add_argument("--company", help="厂商名，英文，至少 3 个字母")
    ap.add_argument("--product", help="产品名、型号或产品 ID，英文，至少 3 个字母")
    ap.add_argument("--type", choices=list(KINDS), default="all", help="成品 finished / 原料 raw / 全部 all")
    ap.add_argument("--max", type=int, default=30, help="最多显示的产品数，默认 30")
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--file", help="解析已保存的结果页（离线测试用），不联网")
    args = ap.parse_args(argv)

    try:
        company = validate_name("厂商名", args.company)
        product = validate_name("产品名", args.product)
        if not company and not product:
            raise ValueError("请至少给出 --company 或 --product 之一")
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    url = build_url(company, product, args.type)
    try:
        if args.file:
            with open(args.file, encoding="utf-8", errors="replace") as fh:
                page = fh.read()
        else:
            page = fetch(url, args.timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"查询失败（没有拿到结果页，这不是「查无」）：{exc}", file=sys.stderr)
        return EXIT_ERROR

    no_match, entries, totals = parse(page)
    if not no_match and not entries:
        print("页面无法解析：既没有「无匹配」提示，也没读到产品行，可能是目录改版或被拦截。"
              "这不是「查无」，请改用浏览器手动查询：" + url, file=sys.stderr)
        return EXIT_ERROR

    info = {"queried_on": datetime.date.today().isoformat(), "company": company, "product": product,
            "kind": KIND_LABEL[args.type], "url": url, "no_match": no_match, "entries": entries,
            "totals": totals, "max": args.max}
    if args.json:
        out = dict(info, note=NOTE_NONE if no_match else NOTE_FOUND)
        out.pop("max")
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render(info))
    return EXIT_NONE if no_match else EXIT_FOUND


if __name__ == "__main__":
    sys.exit(main())
