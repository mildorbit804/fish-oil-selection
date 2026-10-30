"""nsf_lookup.py 与 ifos_lookup.py 的正反用例。不联网：网页内容用 fixtures 里的虚构样例顶替。

重点是反例：目录明确说「查无」才算查无；网络失败、页面改版、被拦截一律报错，不能说成「查无」。
运行：python3 -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import unittest
import urllib.error
from unittest import mock

SKILL = pathlib.Path(__file__).resolve().parents[1]
FIX = pathlib.Path(__file__).resolve().parent / "fixtures"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


def run(mod, argv, fetch):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(mod, "fetch", side_effect=fetch) as patched, \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue(), patched


class NsfLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load("nsf_lookup")

    def test_found_parses_entries_and_skips_header_row(self):
        none_no, entries, _ = self.mod.parse(fixture("nsf_found.html"))
        self.assertFalse(none_no)
        self.assertEqual([e["trade_name"] for e in entries], ["Example Omega 1000", "Example Omega 500"])
        first = entries[0]
        self.assertEqual(first["company"], "Example Marine Oils Ltd.")
        self.assertEqual(first["facility"], "Springfield, XX")
        self.assertEqual(first["standard"], "NSF/ANSI Standard 173 Dietary Supplements")
        self.assertEqual(first["section"], "Finished Products")
        self.assertEqual(first["category"], "Fish Oil/Omega")
        self.assertEqual((first["form"], first["daily_serving"]), ("Softgel", "2 softgels"))

    def test_found_exit_zero(self):
        code, out, _, _ = run(self.mod, ["--company", "example"], lambda url, timeout: fixture("nsf_found.html"))
        self.assertEqual(code, 0)
        self.assertIn("Example Marine Oils Ltd.", out)

    def test_explicit_no_match_exit_three_with_caveat(self):
        code, out, _, _ = run(self.mod, ["--company", "swisse"], lambda url, timeout: fixture("nsf_none.html"))
        self.assertEqual(code, 3)
        self.assertIn("不代表", out)

    def test_unparseable_page_is_error_not_no_match(self):
        code, out, err, _ = run(self.mod, ["--company", "swisse"], lambda url, timeout: fixture("nsf_unparseable.html"))
        self.assertEqual(code, 2)
        self.assertIn("不是「查无」", err)
        self.assertNotIn("无匹配条目", out)

    def test_network_failure_is_error(self):
        def boom(url, timeout):
            raise urllib.error.URLError("连接被拒绝")
        code, _, err, _ = run(self.mod, ["--company", "swisse"], boom)
        self.assertEqual(code, 2)
        self.assertIn("不是「查无」", err)

    def test_short_or_non_english_names_rejected_without_network(self):
        for argv in (["--company", "ab"], ["--company", "斯维诗"], ["--product", "x1"], []):
            code, _, _, patched = run(self.mod, argv, lambda url, timeout: "")
            self.assertEqual(code, 2, argv)
            patched.assert_not_called()

    def test_build_url_encodes_and_sets_type(self):
        url = self.mod.build_url("nordic naturals", "omega & d3", "finished")
        self.assertIn("StandardExt=FP", url)
        self.assertIn("CompanyName=nordic+naturals", url)
        self.assertIn("TradeName=omega+%26+d3", url)

    def test_json_output_is_valid(self):
        code, out, _, _ = run(self.mod, ["--company", "example", "--json"], lambda url, timeout: fixture("nsf_found.html"))
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data["entries"]), 2)
        self.assertFalse(data["no_match"])


class IfosLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load("ifos_lookup")

    @staticmethod
    def site(brands="ifos_brands_found.json", brand_page="ifos_brand_page.html", product_page="ifos_product_page.html"):
        def fetch(url, timeout=25):
            if "GetFilteredBrands" in url:
                return fixture(brands)
            if "/brand?id=" in url:
                return fixture(brand_page)
            if "/product?id=" in url:
                return fixture(product_page)
            raise AssertionError(f"意外的请求：{url}")
        return fetch

    def test_found_lists_ifos_brand_and_skips_non_ifos(self):
        code, out, _, _ = run(self.mod, ["--brand", "example"], self.site())
        self.assertEqual(code, 0)
        self.assertIn("Example Marine Oils Ltd", out)
        self.assertIn("EXAMPLE 4X STRENGTH FISH OIL", out)
        self.assertIn("EXAMPLE KIDS DHA", out)
        self.assertNotIn("Example Probiotics", out)

    def test_product_filter_reads_lots_and_latest_expiry(self):
        code, out, _, _ = run(self.mod, ["--brand", "example", "--product", "4x strength"], self.site())
        self.assertEqual(code, 0)
        self.assertIn("已登记批号 2 个，最晚有效期 2028-01-31（批号 11111A）", out)
        self.assertIn("1200mg of total Omega-3", out)
        self.assertNotIn("EXAMPLE KIDS DHA", out)

    def test_brand_has_ifos_but_product_missing_exit_three_and_lists_registered(self):
        code, out, _, _ = run(self.mod, ["--brand", "example", "--product", "odourless"], self.site())
        self.assertEqual(code, 3)
        self.assertIn("没有名称含「odourless」的产品", out)
        self.assertIn("EXAMPLE KIDS DHA", out)
        self.assertIn("不代表", out)

    def test_no_brand_exit_three_with_caveat(self):
        code, out, _, _ = run(self.mod, ["--brand", "nonexistent"], self.site(brands="ifos_brands_none.json"))
        self.assertEqual(code, 3)
        self.assertIn("不代表", out)

    def test_brand_without_ifos_exit_three_and_names_other_certs(self):
        code, out, _, _ = run(self.mod, ["--brand", "probiotics"], self.site(brands="ifos_brands_noifos.json"))
        self.assertEqual(code, 3)
        self.assertIn("IKOS", out)

    def test_non_json_api_is_error_not_no_match(self):
        code, out, err, _ = run(self.mod, ["--brand", "example"], lambda url, timeout=25: "<html>Access Denied</html>")
        self.assertEqual(code, 2)
        self.assertIn("不是「查无」", err)
        self.assertNotIn("无匹配", out)

    def test_api_success_false_is_error(self):
        code, _, err, _ = run(self.mod, ["--brand", "example"], lambda url, timeout=25: '{"success":false}')
        self.assertEqual(code, 2)
        self.assertIn("不是「查无」", err)

    def test_brand_page_without_products_is_error_not_no_product(self):
        code, out, err, _ = run(self.mod, ["--brand", "example", "--product", "fish"], self.site(brand_page="ifos_brand_page_empty.html"))
        self.assertEqual(code, 2)
        self.assertIn("不是「查无」", err)
        self.assertNotIn("没有名称含", out)

    def test_network_failure_is_error(self):
        def boom(url, timeout=25):
            raise urllib.error.URLError("超时")
        code, _, err, _ = run(self.mod, ["--brand", "example"], boom)
        self.assertEqual(code, 2)
        self.assertIn("不是「查无」", err)

    def test_short_or_non_english_brand_rejected_without_network(self):
        for argv in (["--brand", "ab"], ["--brand", "斯维诗"], ["--brand", "example", "--product", "x"]):
            code, _, _, patched = run(self.mod, argv, lambda url, timeout=25: "")
            self.assertEqual(code, 2, argv)
            patched.assert_not_called()

    def test_json_output_is_valid(self):
        code, out, _, _ = run(self.mod, ["--brand", "example", "--product", "4x strength", "--json"], self.site())
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["status"], "found")
        self.assertEqual(data["brands"][0]["products"][0]["lots"][0]["lot"], "11111A")


class SkillWiringTests(unittest.TestCase):
    def test_skill_md_mentions_both_scripts(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for name in ("scripts/nsf_lookup.py", "scripts/ifos_lookup.py"):
            self.assertIn(name, text, f"SKILL.md 没写到 {name}")

    def test_skill_md_keeps_video_standard_section(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("## 8. 视频的选购标准（对照项）", text)
        section = text.split("## 8. 视频的选购标准（对照项）", 1)[1]
        for needle in ("EPA＋DHA", "30%", "50%", "85%", "任意一个", "USP", "NSF", "IFOS", "按视频标准", "不是医学或监管分级"):
            self.assertIn(needle, section, f"第 8 节缺少：{needle}")

    def test_skill_md_keeps_pre_purchase_rules(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for needle in ("买前调研", "不索要批号", "按截图标价", "不向用户追问到手价", "买前无法核对", "不列批号和到手价"):
            self.assertIn(needle, text, f"SKILL.md 缺少买前调研规则：{needle}")

    def test_sources_md_points_to_video_standard_section(self):
        text = (SKILL / "references" / "sources.md").read_text(encoding="utf-8")
        self.assertIn("第 8 节", text)


if __name__ == "__main__":
    unittest.main()
