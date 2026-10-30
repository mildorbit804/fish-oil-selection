#!/usr/bin/env python3
"""查询 IFOS（SGS Nutrasource）公开目录里某个品牌、产品有没有登记。

只回答「IFOS 目录里按这个名字查到了什么」。目录里没有，不等于没检测过；
网页打不开或页面结构变了，一律报错退出，不当成「查无」。

用法示例：
  python3 scripts/ifos_lookup.py --brand swisse
  python3 scripts/ifos_lookup.py --brand swisse --product "wild fish oil"   # 同时读批号与有效期

退出码：0 查到；3 目录明确查无；2 参数错误、网络失败或页面无法解析。
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

ROOT = "https://certifications.nutrasource.ca"
BRANDS_API = ROOT + "/umbraco/surface/NutrasourceContent/GetFilteredBrands"
EXIT_FOUND, EXIT_ERROR, EXIT_NONE = 0, 2, 3
MAX_BRANDS = 5
MAX_DETAIL = 15
OTHER_CERTS = {"HasIkos": "IKOS", "HasIgen": "IGEN", "HasIpro": "IPRO", "HasIcap": "ICAP", "HasIAOS": "IAOS"}

NOTE_FOUND = (
    "目录登记的是品牌名下的具体产品，需与手上这一瓶的产品名、规格逐项对照；"
    "品牌有 IFOS 登记，不代表这一瓶也在内。"
)
NOTE_NONE = "这只说明 IFOS 目录里没有登记，不代表产品没检测过或不合格；也可能登记在别的品牌名下。"


class PageError(Exception):
    """页面或接口返回的内容无法按预期解析。"""


def clean(fragment):
    text = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def validate_name(label, value):
    if value is None:
        return None
    if len(re.findall(r"[A-Za-z0-9]", value)) < 3:
        raise ValueError(f"{label}需至少 3 个英文字母或数字（IFOS 目录用英文名检索）：{value!r}")
    return value.strip()


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fish-oil-selection ifos_lookup)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def search_brands(name):
    url = BRANDS_API + "?" + urllib.parse.urlencode({"pageNumber": 1, "pageSize": 50, "byName": name})
    try:
        data = json.loads(fetch(url))
    except ValueError as exc:
        raise PageError(f"品牌接口返回的不是 JSON：{exc}") from exc
    if not isinstance(data, dict) or data.get("success") is not True or not isinstance(data.get("list"), list):
        raise PageError("品牌接口返回的结构不符合预期")
    brands = []
    for item in data["list"]:
        brands.append({
            "id": item.get("BrandId"),
            "name": clean(item.get("Name") or ""),
            "has_ifos": bool(item.get("HasIfos")),
            "other_certs": [label for key, label in OTHER_CERTS.items() if item.get(key)],
        })
    return brands, data.get("totalCount", len(brands)), url


def parse_brand_page(page):
    """品牌页 → [{id, name, certs}]。读不到任何产品就报错，避免把改版当成「没有产品」。"""
    products = []
    for chunk in page.split('<div class="col-6 col-lg-4 col-xl-3 d-flex flex-column">'):
        m = re.search(r'<h3 class="results__brand[^"]*"><strong><a href="/certified-products/product\?id=([^"]+)">(.*?)</a>', chunk, re.S)
        if not m:
            continue
        certs = []
        for c in re.findall(r'alt="Certified ([A-Za-z]+)"', chunk):
            if c not in certs:
                certs.append(c)
        products.append({"id": m.group(1), "name": clean(m.group(2)), "certs": certs})
    if not products:
        raise PageError("品牌页里没读到任何产品")
    return products


def parse_product_page(page):
    """产品页 → {summary, lots:[{lot, expires}]}。"""
    m = re.search(r"<h2>(.*?)</h2>\s*<h3>Product Summary</h3>\s*<p>(.*?)</p>", page, re.S)
    if not m:
        raise PageError("产品页里没读到产品摘要")
    text = clean(page)
    lots = []
    for lot, date in re.findall(r"Lot #:\s*(\S+)\s*\(([A-Za-z]{3} \d{1,2}, \d{4})\)", text):
        try:
            date = datetime.datetime.strptime(date, "%b %d, %Y").date().isoformat()
        except ValueError:
            pass
        lots.append({"lot": lot, "expires": date})
    return {"summary": clean(m.group(2)), "lots": lots}


def lookup(brand_query, product_query, want_detail):
    brands, total, url = search_brands(brand_query)
    result = {"brand_url": url, "brands_total": total, "brands": [], "no_ifos_brands": []}
    for b in brands:
        if not b["has_ifos"]:
            result["no_ifos_brands"].append(b)
    ifos_brands = [b for b in brands if b["has_ifos"]][:MAX_BRANDS]
    detail_budget = MAX_DETAIL
    for b in ifos_brands:
        page_url = f"{ROOT}/certified-products/brand?id={urllib.parse.quote(str(b['id']))}"
        products = parse_brand_page(fetch(page_url))
        b = dict(b, page_url=page_url, all_products=products, products=products)
        if product_query:
            b["products"] = [p for p in products if product_query.lower() in p["name"].lower()]
        if product_query or want_detail:
            for p in b["products"]:
                if detail_budget <= 0:
                    break
                detail_budget -= 1
                p.update(parse_product_page(fetch(f"{ROOT}/certified-products/product?id={urllib.parse.quote(p['id'])}")))
        result["brands"].append(b)
    return result


def render(info, result):
    lines = [
        f"IFOS 目录查询（certifications.nutrasource.ca，查询日期 {info['queried_on']}）",
        f"条件：品牌含「{info['brand']}」" + (f"，产品含「{info['product']}」" if info["product"] else ""),
    ]
    if info["status"] == "no_brand":
        lines += ["结果：品牌检索无匹配。", NOTE_NONE]
    elif info["status"] == "no_ifos":
        lines.append("结果：找到品牌，但都没有 IFOS 登记：")
        lines += [f"  - {b['name']}（{b['id']}）｜其他认证：{'、'.join(b['other_certs']) or '无'}" for b in result["no_ifos_brands"]]
        lines.append(NOTE_NONE)
    elif info["status"] == "no_product":
        lines.append(f"结果：品牌有 IFOS 登记，但没有名称含「{info['product']}」的产品。该品牌名下登记的产品：")
        for b in result["brands"]:
            lines.append(f"  {b['name']}（{b['id']}）")
            lines += [f"    · {p['name']}（{p['id']}）" for p in b["all_products"]]
        lines += ["", NOTE_NONE]
    else:
        lines.append(f"结果：{len(result['brands'])} 个品牌有 IFOS 登记。")
        for b in result["brands"]:
            if info["product"] and not b["products"]:
                continue
            lines.append(f"\n{b['name']}（{b['id']}）｜名下产品 {len(b['all_products'])} 个，显示 {len(b['products'])} 个")
            for p in b["products"]:
                lines.append(f"  · {p['name']}（{p['id']}）｜认证：{'、'.join(p['certs']) or '未标'}")
                if "summary" in p:
                    lines.append(f"    摘要：{p['summary'][:240]}")
                    if p["lots"]:
                        last = max(p["lots"], key=lambda x: x["expires"])
                        lines.append(f"    已登记批号 {len(p['lots'])} 个，最晚有效期 {last['expires']}（批号 {last['lot']}）")
                    else:
                        lines.append("    已登记批号：无")
            lines.append(f"  品牌页：{b['page_url']}")
        lines += ["", NOTE_FOUND]
    if result["brands_total"] and result["brands_total"] > 50:
        lines.append(f"品牌匹配 {result['brands_total']} 个，只看了前 50 个，请缩小品牌名。")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="查询 IFOS 公开目录里的品牌与产品登记")
    ap.add_argument("--brand", required=True, help="品牌名，英文，至少 3 个字母")
    ap.add_argument("--product", help="产品名关键词；给了就同时读批号与有效期")
    ap.add_argument("--detail", action="store_true", help="对列出的产品逐个读批号与有效期（最多 15 个）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    try:
        brand = validate_name("品牌名", args.brand)
        product = validate_name("产品名", args.product)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        result = lookup(brand, product, args.detail)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"查询失败（没有拿到结果，这不是「查无」）：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except PageError as exc:
        print(f"页面无法解析：{exc}。这不是「查无」，请改用浏览器手动查询：{ROOT}/certified-products", file=sys.stderr)
        return EXIT_ERROR

    if not result["brands"] and not result["no_ifos_brands"]:
        status = "no_brand"
    elif not result["brands"]:
        status = "no_ifos"
    elif product and not any(b["products"] for b in result["brands"]):
        status = "no_product"
    else:
        status = "found"
    info = {"queried_on": datetime.date.today().isoformat(), "brand": brand, "product": product, "status": status}
    if args.json:
        print(json.dumps(dict(info, **result, note=NOTE_FOUND if status == "found" else NOTE_NONE),
                         ensure_ascii=False, indent=2))
    else:
        print(render(info, result))
    return EXIT_FOUND if status == "found" else EXIT_NONE


if __name__ == "__main__":
    sys.exit(main())
