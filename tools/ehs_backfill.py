"""EHS 待补清单批量补库工具

读取《EHS待补清单》xlsx，把缺失/过期文件从官方平台下载到本地知识库，
按知识库既有目录结构和命名规则落盘，支持断点续跑。

用法:
  python tools/ehs_backfill.py --xlsx "清单.xlsx" --lanes laws --limit 20
  python tools/ehs_backfill.py --xlsx "清单.xlsx" --lanes laws,db,tt
  python tools/ehs_backfill.py --xlsx "清单.xlsx" --lanes hb
  python tools/ehs_backfill.py --xlsx "清单.xlsx" --lanes laws --filter "条例" --retry

数据来源:
  法律法规  https://flk.npc.gov.cn        （Playwright 浏览器内调用接口）
  国家标准  https://std.samr.gov.cn / openstd.samr.gov.cn
  行业标准  https://hbba.sacinfo.org.cn
  地方标准  https://dbba.sacinfo.org.cn
  团体标准  https://www.ttbz.org.cn

状态与报告:
  ~/.std_scanner/ehs_backfill_state.json
  ~/.std_scanner/ehs_backfill_report.csv
"""
import argparse
import asyncio
import csv
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import openpyxl

from app.helpers import atomic_write, normalize_code, safe_filename
from app.scanner.utils import make_filename

LIB_ROOT = Path(r"E:\标准规范\标准规范-2025\法律法规、标准规范")
LAW_DIR = LIB_ROOT / "法律法规、政府规章、规定通知"
STATE_DIR = Path.home() / ".std_scanner"
STATE_FILE = STATE_DIR / "ehs_backfill_state.json"
REPORT_FILE = STATE_DIR / "ehs_backfill_report.csv"

SHEETS = [
    "①法律", "②行政法规", "③部门规章", "④规范性文件",
    "⑤国家标准", "⑥行业标准", "⑦地方标准-苏沪浙皖", "⑧团体标准", "⑨地方法规-苏沪浙皖",
]

LAW_SHEETS = {"①法律", "②行政法规", "④规范性文件", "⑨地方法规-苏沪浙皖"}

FOLDER_MAP = {
    "AQ": "AQ 安全生产", "AQT": "AQ 安全生产",
    "CJJ": "CJJ 城建", "CJJT": "CJJ 城建",
    "DL": "DL 电力", "DLT": "DL 电力",
    "GA": "GA 公安", "GAT": "GA 公安", "GAZ": "GA 公安",
    "GB": "GB 国家标准", "GBT": "GB 国家标准", "GBZ": "GB 国家标准",
    "GBZT": "GB 国家标准", "GBJ": "GB 国家标准",
    "JGJ": "JGJ 建筑工业", "JGJT": "JGJ 建筑工业",
    "KA": "KA 矿产", "KAT": "KA 矿产",
    "LD": "LD 劳动", "LDT": "LD 劳动",
    "MT": "MT 煤炭", "MTT": "MT 煤炭",
    "NB": "NB 能源", "NBT": "NB 能源",
    "SY": "SY 石油天然气", "SYT": "SY 石油天然气",
    "TSG": "TSG 特种设备",
    "XF": "XF 消防", "XFT": "XF 消防",
    "YJ": "YJ 应急", "YJT": "YJ 应急",
    "DGJ": "DGJ08 上海工程建设", "DGTJ": "DGJ08 上海工程建设",
}

TERMINAL_OK = {"downloaded", "skipped_existing"}
TERMINAL_SKIP = {"no_fulltext", "copyright", "not_found", "parse_error"}


# ==================== 清单解析 ====================
def parse_std(text):
    """从文件名称中解析标准编号，返回 (字母编号, 顺序号, 年份) 或 None。

    例: 'GB/T 10827.1-2014 xx' -> ('GBT', '10827.1', '2014')
        'DB 34/T 4655-2024'     -> ('DB34T', '4655', '2024')
        'T_CAWS 0020-2024'      -> ('TCAWS', '0020', '2024')
    """
    s = text.upper().replace("（", "(").replace("）", ")")
    s = s.replace("／", "/").replace("—", "-").replace("－", "-").replace("–", "-")
    s = s.replace("＋", "+").replace("+", " ")
    s = re.sub(r"^(T)\s*[-_/]\s*", r"\1", s)
    s = re.sub(r"(DB)\s*(\d{2})\s*[-/]?\s*T\b", r"\1\2T", s)
    s = re.sub(r"^([A-Z]{2,4})\s+([A-Z][A-Z0-9]*)", r"\1\2", s)
    s = re.sub(r"\.\s+", ".", s)
    s = s.replace("/", "").replace("_", "")
    s = re.sub(r"\s+", " ", s).strip()
    m = re.search(r"([A-Z][A-Z0-9]*?)\s*([0-9][0-9.]*(?:-[0-9]+)*)\s*-\s*([0-9]{2,4})", s)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def code_key(code, num):
    """标准编号比对键（去空格/斜杠/点号，不含年份）"""
    return re.sub(r"[^A-Z0-9]", "", f"{code}{num}".upper())


def strip_law_suffix(text):
    """去掉标题中的书名号和“（2019年修正）”“（2024修订）”“（国务院令第799号）”等说明"""
    s = text.strip()
    s = s.replace("(", "（").replace(")", "）")
    s = re.sub(r"^《(.*)》$", r"\1", s)
    s = re.sub(r"（[^）]*令[^）]*）", "", s)
    s = re.sub(r"（[0-9]{4}[^）]*(?:修正|修订|发布|通过)[^）]*）", "", s)
    s = s.replace("《", "").replace("》", "")
    return s.strip()


def norm_title(text):
    """法律标题归一化，用于和本地文件名比对"""
    s = Path(text).stem
    s = re.sub(r"_[0-9]{8}$", "", s)
    s = strip_law_suffix(s)
    s = re.sub(r"[\s\u3000]", "", s)
    s = re.sub(r"[《》“”\"'‘’（）()\[\]【】、，,。.．:：;；\-—_/]", "", s)
    return s.lower()


def leading_letters(code):
    m = re.match(r"[A-Z]+", code)
    return m.group(0) if m else ""


def title_part(name):
    """从“编号 + 名称”中取出纯名称部分"""
    s = re.sub(r"^.*?[0-9]{4}\s*", "", name, count=1).strip()
    return s or name


def target_folder(code, kind):
    if kind == "law":
        return LAW_DIR
    if kind == "db":
        return LIB_ROOT / "DB 省市地方标准"
    if kind == "tt":
        return LIB_ROOT / "团体标准"
    if kind == "gb":
        return LIB_ROOT / "GB 国家标准"
    letters = leading_letters(code)
    if letters.startswith("TSG"):
        return LIB_ROOT / "TSG 特种设备"
    return LIB_ROOT / FOLDER_MAP.get(letters, "其他行业标准")


def display_code(code, num):
    """生成用于平台检索的编号文本（还原 GB/T、DB33/T、T/CCSAS 等写法）"""
    if re.match(r"DB\d{2}T$", code):
        return f"{code[:4]}/T {num}"
    if code.startswith("T") and len(code) > 1 and not code.endswith("T"):
        return f"T/{code[1:]} {num}"
    if code.endswith("T") and len(code) >= 3:
        return f"{code[:-1]}/T {num}"
    return f"{code} {num}"


# ==================== 本地已有文件索引 ====================
def load_existing():
    """扫描知识库，建立标准编号与法律标题索引"""
    std_index = defaultdict(set)
    law_index = defaultdict(set)
    for root, _dirs, files in os.walk(LIB_ROOT):
        for f in files:
            p = Path(root) / f
            if p.parent == LAW_DIR:
                m = re.search(r"_(\d{8})$", Path(f).stem)
                law_index[norm_title(f)].add(m.group(1) if m else "")
            parsed = parse_std(f)
            if parsed:
                code, num, year = parsed
                std_index[code_key(code, num)].add(year)
    return std_index, law_index


def version_year(text):
    """从清单名称中提取版本年份，如（2019年修正）→ 2019"""
    m = re.search(r"[（(]([0-9]{4})", text)
    return m.group(1) if m else ""


def law_already_exists(item, law_index):
    """本地已有同名法律，且版本年份一致（或清单未标注版本）时才视为已存在"""
    years = law_index.get(norm_title(item["name"]))
    if not years:
        return False
    want = version_year(item["name"])
    return any(y[:4] == want for y in years) if want else True


# ==================== 清单条目 ====================
def load_items(xlsx_path):
    """读取清单，返回按下载通道分组的条目列表"""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    items = []
    seen = set()
    for sheet in SHEETS:
        ws = wb[sheet]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[1]:
                continue
            name = str(row[1]).strip()
            loc = str(row[9]).strip() if len(row) > 9 and row[9] else ""
            if sheet in LAW_SHEETS:
                kind = "law"
                parsed = None
            elif sheet == "③部门规章":
                kind = "mem"
                parsed = None
            elif sheet == "⑤国家标准":
                kind, parsed = "gb", parse_std(name)
            elif sheet == "⑦地方标准-苏沪浙皖":
                kind, parsed = "db", parse_std(name)
            elif sheet == "⑧团体标准":
                kind, parsed = "tt", parse_std(name)
            else:
                parsed = parse_std(name)
                if not parsed:
                    kind = "law"
                else:
                    letters = leading_letters(parsed[0])
                    kind = "gb" if letters in ("GB", "GBT", "GBZ", "GBZT", "GBJ") else "hb"

            if kind in ("law", "mem"):
                if name.startswith(("事项", "主项")) and ("实施要素" in name or name.endswith("下子项")):
                    key = f"unsupported|{norm_title(name)}"
                    if key not in seen:
                        seen.add(key)
                        items.append({"key": key, "sheet": sheet, "name": name, "kind": "unsupported",
                                      "loc": loc, "reason": "行政服务事项说明，非法规文件"})
                    continue
                key = f"{kind}|{norm_title(name)}"
                if key in seen:
                    continue
                seen.add(key)
                items.append({"key": key, "sheet": sheet, "name": name, "kind": kind,
                              "title": strip_law_suffix(name), "loc": loc})
                continue

            if not parsed:
                key = f"skip|{name}"
                if key in seen:
                    continue
                seen.add(key)
                items.append({"key": key, "sheet": sheet, "name": name, "kind": "parse_error",
                              "loc": loc, "reason": "无法解析标准编号"})
                continue

            code, num, year = parsed
            key = f"{kind}|{code_key(code, num)}|{year}"
            if key in seen:
                continue
            seen.add(key)
            items.append({"key": key, "sheet": sheet, "name": name, "kind": kind,
                          "code": code, "num": num, "year": year, "loc": loc})
    return items


# ==================== 通用下载 ====================
def validate_file(data, ext):
    if not data or len(data) < 1000:
        return False
    if ext == ".pdf":
        return data[:5] == b"%PDF-"
    if ext in (".docx", ".doc"):
        return data[:4] == b"PK\x03\x04" or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    return True


def save_file(data, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(str(path), data, dir_=str(path.parent))
    return path.stat().st_size


# ==================== 法律（flk.npc.gov.cn） ====================
class LawDownloader:
    """在 Playwright 浏览器上下文中调用 flk 接口，规避 WAF"""

    def __init__(self):
        self._pw = None
        self._browser = None
        self.ctx = None
        self.page = None
        self.calls = 0

    async def start(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self.ctx = await self._browser.new_context(accept_downloads=True)
        self.page = await self.ctx.new_page()
        await self.page.goto("https://flk.npc.gov.cn/", wait_until="networkidle", timeout=60000)
        self.calls = 0

    async def restart(self):
        """flk 的 WAF 会在连续请求后拦截，重建浏览器会话即可恢复"""
        await self.close()
        await self.start()

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None

    async def _api(self, script, arg):
        """调用页面内接口；返回 HTML（WAF 拦截）时重建会话重试一次"""
        if self.calls >= 40:
            await self.restart()
        self.calls += 1
        for attempt in (1, 2):
            try:
                result = await self.page.evaluate(script, arg)
                if isinstance(result, dict) and not result.get("__error__"):
                    return result
            except Exception:
                pass
            if attempt == 1:
                await self.restart()
                await asyncio.sleep(2)
        return {"__error__": True}

    async def search(self, title):
        body = {
            "searchRange": 1, "sxrq": [], "gbrq": [], "searchType": 1, "sxx": [],
            "gbrqYear": [], "flfgCodeId": [], "zdjgCodeId": [],
            "searchContent": title, "page": 1, "size": 30,
        }
        data = await self._api(
            """async (body) => {
                const r = await fetch('/law-search/search/list', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json;charset=utf-8'},
                    body: JSON.stringify(body)
                });
                if (!r.ok) return {__error__: true};
                const ct = r.headers.get('content-type') || '';
                if (!ct.includes('json')) return {__error__: true};
                try { return await r.json(); } catch (e) { return {__error__: true}; }
            }""", body)
        if data.get("__error__"):
            raise RuntimeError("flk 接口被 WAF 拦截")
        rows = data.get("rows") or []
        out = []
        for x in rows:
            t = re.sub(r"<em class='highlight'>", "", x.get("title", "")).replace("</em>", "").strip()
            out.append({"title": t, "gbrq": x.get("gbrq") or "", "sxx": x.get("sxx"),
                        "bbbs": x.get("bbbs"), "flxz": x.get("flxz")})
        return out

    async def get_detail(self, bbbs):
        data = await self._api(
            """async (bbbs) => {
                const r = await fetch('/law-search/search/flfgDetails?bbbs=' + encodeURIComponent(bbbs));
                if (!r.ok) return {__error__: true};
                const ct = r.headers.get('content-type') || '';
                if (!ct.includes('json')) return {__error__: true};
                try { return await r.json(); } catch (e) { return {__error__: true}; }
            }""", bbbs)
        if data.get("__error__"):
            raise RuntimeError("flk 详情接口被 WAF 拦截")
        return data

    async def download(self, bbbs):
        """返回 (data, ext, official_title, official_date)"""
        detail = await self.get_detail(bbbs)
        info = (detail.get("data") or {})
        title = info.get("title") or ""
        gbrq = info.get("gbrq") or ""
        oss = info.get("ossFile") or {}
        for fmt, ext in (("word", ".docx"), ("pdf", ".pdf")):
            if fmt == "word" and not oss.get("ossWordPath"):
                continue
            if fmt == "pdf" and not oss.get("ossPdfPath"):
                continue
            r = await self.ctx.request.get(
                f"https://flk.npc.gov.cn/law-search/download/pc?bbbs={bbbs}&format={fmt}", timeout=60000)
            try:
                js = json.loads(await r.text())
            except Exception:
                continue
            if js.get("code") != 200:
                continue
            url = (js.get("data") or {}).get("url")
            if not url:
                continue
            rr = await self.ctx.request.get(url, timeout=120000)
            data = await rr.body()
            if validate_file(data, ext):
                return data, ext, title, gbrq
        return None, "", "", ""

    async def fetch_one(self, item, out_dir):
        """下载一条法律，返回 (status, reason, path)"""
        title = item["title"]
        rows = await self.search(title)
        exact = [r for r in rows if r["sxx"] == 3 and r["title"] == title]
        if not exact:
            exact = [r for r in rows if r["sxx"] == 3 and r["title"].startswith(title)]
        if not exact:
            # 去掉书名号后再试一次
            alt = re.sub(r"[《》]", "", title)
            if alt != title:
                rows = await self.search(alt)
                exact = [r for r in rows if r["sxx"] == 3 and r["title"] == alt]
        if not exact:
            return "not_found", "官方库未检索到有效版本", None

        row = sorted(exact, key=lambda x: x["gbrq"], reverse=True)[0]
        data, ext, official_title, gbrq = await self.download(row["bbbs"])
        if not data:
            return "no_fulltext", "官方未提供可下载文件", None
        filename = safe_filename(f"{official_title or title}_{gbrq.replace('-', '')}{ext}")
        path = out_dir / filename
        save_file(data, path)
        return "downloaded", f"官方发布日期 {gbrq}", path


# ==================== 标准平台 ====================
def fetch_gb_candidates(num, code_display):
    from config.settings import http_client
    for text in (num, code_display):
        try:
            r = http_client.get("https://std.samr.gov.cn/gb/search/gbQueryPage", params={
                "pageNumber": 1, "pageSize": 50, "sortOrder": "desc", "sortName": "id",
                "searchText": text, "state": 'G_STATE:"现行"'})
            rows = r.json().get("rows", [])
            if rows:
                return rows
        except Exception:
            continue
    return []


def download_gb(item):
    """国家标准：目录 API → 详情页 hcno → openstd 下载"""
    from config.settings import http_client, DETAIL_URL, OPENSTD
    from app.scanner.download_helpers import extract_hcno_from_html, detect_download_buttons
    from app.scanner.download import download_with_captcha

    code, num, year = item["code"], item["num"], item["year"]
    rows = fetch_gb_candidates(num, display_code(code, num))
    target = None
    for row in rows:
        parsed = parse_std(normalize_code(row.get("C_STD_CODE", "")))
        if parsed and code_key(parsed[0], parsed[1]) == code_key(code, num) and parsed[2] == year:
            target = row
            break
    if not target:
        return "not_found", "官方目录未找到同年份版本", None

    official_code = normalize_code(target.get("C_STD_CODE", ""))
    official_name = target.get("C_C_NAME", "").strip()
    r = http_client.get(f"{DETAIL_URL}?id={target['id']}")
    hcno = extract_hcno_from_html(r.text)
    if not hcno:
        return "not_found", "详情页未找到 hcno", None

    html, used = "", ""
    for case in (hcno, hcno.lower()):
        for path in ("std", "gb"):
            url = f"{OPENSTD}/{path}/newGbInfo?hcno={case}"
            resp = http_client.get(url)
            if resp.text and "尚未收录" not in resp.text:
                html, used = resp.text, case
                break
        if html:
            break
    if not html:
        return "blocked", "openstd 详情页返回空（官方全文服务异常）", None
    if "尚未收录" in html:
        return "no_fulltext", "openstd 尚未收录该标准全文", None

    btns = detect_download_buttons(html)
    if not btns.can_download:
        return ("copyright", "版权限制，不提供下载", None) if btns.copyright else \
               ("no_fulltext", "无下载按钮（仅预览或未公开）", None)

    data = download_with_captcha(used or hcno, reason_out={})
    if not data:
        return "failed", "验证码下载失败", None
    filename = make_filename(official_code, official_name)
    path = target_folder(code, "gb") / filename
    save_file(data, path)
    return "downloaded", f"{official_code} {official_name}", path


# ==================== 部门规章（应急管理部规章库） ====================
_MEM_RULES = []


def _load_mem_rules():
    """抓取应急管理部规章库（75 条，含 PDF 下载链接），只取一次"""
    global _MEM_RULES
    if _MEM_RULES:
        return _MEM_RULES
    import httpx
    from config.settings import MEM_GZ_LIST_URL
    from app.scanner.mem_scan import _gz_parse_list_items, _mem_headers

    headers = _mem_headers("gz")
    rows = []
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as client:
        for p in range(1, 6):
            url = MEM_GZ_LIST_URL if p == 1 else MEM_GZ_LIST_URL.replace("index.shtml", f"index_{p-1}.shtml")
            try:
                resp = client.get(url)
                rows.extend(_gz_parse_list_items(resp.text))
            except Exception:
                continue
    _MEM_RULES = rows
    return rows


def _rule_date(url):
    """从 PDF 文件名/P0 日期或 URL 年月提取 YYYYMMDD"""
    m = re.search(r"P0(\d{8})", url or "")
    if m:
        return m.group(1)
    m = re.search(r"/(\d{6})/", url or "")
    return (m.group(1) + "01") if m else ""


def download_mem(item):
    """部门规章：应急管理部规章库按标题匹配并下载 PDF"""
    import httpx
    from app.scanner.mem_scan import _mem_headers

    rules = _load_mem_rules()
    target_title = norm_title(item["name"])
    candidates = []
    for r in rules:
        if r.get("status") == "废止" or "已废止" in r.get("name", ""):
            continue
        nt = norm_title(r["name"])
        if nt == target_title or (len(target_title) >= 8 and (target_title in nt or nt in target_title)):
            candidates.append(r)
    if not candidates:
        return "not_found", "应急管理部规章库未匹配到该规章", None

    target = sorted(candidates, key=lambda r: _rule_date(r.get("pdf_url", "")), reverse=True)[0]
    url = target.get("pdf_url")
    if not url:
        return "no_fulltext", "规章库未提供下载链接", None
    try:
        with httpx.Client(timeout=60, headers=_mem_headers("gz"), follow_redirects=True) as client:
            data = client.get(url).content
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}", None
    if not validate_file(data, ".pdf"):
        return "failed", "下载内容不是有效 PDF", None
    # 规章库 PDF 文件名中的日期是网站上传日期，不是公布日期；
    # 直接沿用清单名称（含“（2015年修正）”等版本说明），避免写入错误日期。
    filename = safe_filename(f"{item['name']}.pdf")
    path = LAW_DIR / filename
    save_file(data, path)
    return "downloaded", f"{target['name']}（应急管理部规章库）", path


# ==================== 行业/地方标准 ====================
def _search_hbdb(site, item, max_calls=4):
    from app.scanner.hb_scan import fetch_hb_list
    from app.scanner.db_scan import fetch_db_list

    code, num = item["code"], item["num"]
    display = display_code(code, num)
    variants = [display, f"{display}-{item['year']}", num, title_part(item["name"])]
    records = []
    for key in variants[:max_calls]:
        try:
            if site == "hb":
                r = fetch_hb_list(industry="", key=key, status="", page=1, size=30)
            else:
                r = fetch_db_list(province="", key=key, status="", page=1, size=30)
            recs = r.get("records", [])
            records.extend(recs)
            if any(_match_std(x.get("code", ""), item) for x in recs):
                break
        except Exception:
            continue
        time.sleep(1.2)
    return records


def _match_std(site_code, item):
    parsed = parse_std(normalize_code(site_code))
    if not parsed:
        return False
    return code_key(parsed[0], parsed[1]) == code_key(item["code"], item["num"]) and parsed[2] == item["year"]


def download_hbdb(site, item):
    """行业/地方标准：hbba/dbba 检索 → pk → PDF"""
    import httpx
    from app.scanner.hb_scan import download_hb_with_captcha, CopyrightError, _is_copyright_restricted
    from app.helpers import normalize_code as nc

    records = _search_hbdb(site, item)
    target = next((x for x in records if _match_std(x.get("code", ""), item)), None)
    if not target:
        return "not_found", "官方平台未检索到同年份版本", None

    base_url = "https://hbba.sacinfo.org.cn" if site == "hb" else "https://dbba.sacinfo.org.cn"
    detail_url = f"{base_url}/stdDetail/{target['pk']}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": detail_url,
    }
    client = httpx.Client(timeout=25, headers=headers, follow_redirects=True)
    reason = {}
    try:
        # 先探测“尚未公开”，避免版权件进入 3 次重试
        if _is_copyright_restricted(client, base_url, target["pk"], detail_url):
            return "copyright", "版权限制，不公开全文", None
        data = download_hb_with_captcha(target["pk"], site, client=client, reason_out=reason)
    except CopyrightError:
        return "copyright", "版权限制，不公开全文", None
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}", None
    finally:
        client.close()
    if not data:
        return "failed", reason.get("reason", "下载失败"), None
    official_code = nc(target.get("code", ""))
    official_name = (target.get("chName") or "").strip()
    filename = make_filename(official_code, official_name)
    path = target_folder(item["code"], item["kind"]) / filename
    save_file(data, path)
    return "downloaded", f"{official_code} {official_name}", path


def download_tt(item):
    """团体标准：ttbz 列表 → 详情 → PDF"""
    import httpx
    from urllib.parse import urlencode
    from config.settings import TT_BASE

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": f"{TT_BASE}/standard.html",
        "Origin": TT_BASE,
    }
    list_url = f"{TT_BASE}/cms-proxy/ms/portal/standardInfo/getPortalStandardList"
    detail_url = f"{TT_BASE}/cms-proxy/ms/portal/standardInfo/getPortalStandardById"

    code, num = item["code"], item["num"]
    display = display_code(code, num)
    target = None
    with httpx.Client(timeout=30, headers=headers) as client:
        for key in (display, f"{display}-{item['year']}", title_part(item["name"])):
            body = urlencode({"pageNo": 1, "pageSize": 50, "searchKey": key})
            try:
                js = client.post(list_url, content=body.encode()).json()
            except Exception:
                continue
            for row in (js.get("data") or {}).get("rows") or []:
                if _match_std(row.get("standardNo", ""), item):
                    target = row
                    break
            if target:
                break
            time.sleep(1.2)

        if not target:
            return "not_found", "团体标准平台未检索到同年份版本", None

        body = urlencode({"standardUniqueId": target.get("standardUniqueId", "")})
        detail = client.post(detail_url, content=body.encode()).json()
        file_url = ""
        for f in (detail.get("data") or {}).get("files") or []:
            # fileType=2 是“公告文件”，不是标准正文，不能当作标准保存
            if f.get("fileType") == 2 or "公告" in (f.get("fileTypeName") or ""):
                continue
            if f.get("fileUrl", "").lower().endswith(".pdf"):
                file_url = f["fileUrl"]
                break
        if not file_url:
            return "no_fulltext", "平台仅有公告文件，无标准正文", None

        full = file_url if file_url.startswith("http") else f"{TT_BASE}{file_url}"
        try:
            data = client.get(full, headers={"Referer": f"{TT_BASE}/standard.html"}).content
        except Exception as e:
            return "failed", f"{type(e).__name__}: {e}", None

    if not validate_file(data, ".pdf"):
        return "no_fulltext", "下载内容不是有效 PDF", None
    official_code = normalize_code(target.get("standardNo", "")) or code
    official_name = target.get("standardTitleCn") or item["name"]
    filename = make_filename(official_code, official_name)
    path = target_folder(code, "tt") / filename
    save_file(data, path)
    return "downloaded", f"{official_code} {official_name}", path


# ==================== 状态与报告 ====================
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(str(STATE_FILE), json.dumps(state, ensure_ascii=False, indent=1), mode="w")


def append_report(row):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    new = not REPORT_FILE.exists()
    with open(REPORT_FILE, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["时间", "工作表", "清单文件名称", "状态", "说明", "落盘文件"])
        w.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="EHS 待补清单批量补库")
    parser.add_argument("--xlsx", required=True, help="待补清单 xlsx 路径")
    parser.add_argument("--lanes", default="laws,mem,db,tt,hb,gb",
                        help="下载通道，逗号分隔：laws,mem,db,tt,hb,gb")
    parser.add_argument("--limit", type=int, default=0, help="每个通道最多处理条数，0=不限")
    parser.add_argument("--filter", default="", help="仅处理名称包含该关键词的条目")
    parser.add_argument("--delay", type=float, default=2.0, help="条目间隔秒数")
    parser.add_argument("--concurrent", type=int, default=3, help="行业标准下载并发数（1-5）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不下载")
    parser.add_argument("--retry", action="store_true", help="重试上次失败/阻断/未找到的条目")
    parser.add_argument("--retry-failed", action="store_true", help="只重试上次 failed/blocked 的条目")
    args = parser.parse_args()

    lanes = [x.strip() for x in args.lanes.split(",") if x.strip()]
    items = load_items(args.xlsx)
    if args.filter:
        items = [x for x in items if args.filter.lower() in x["name"].lower()]
    std_index, law_index = load_existing()
    state = load_state()

    # 统计与已有文件过滤
    todo = []
    for it in items:
        if it["kind"] == "parse_error":
            state.setdefault(it["key"], {"status": "parse_error", "reason": it["reason"]})
            continue
        if it["kind"] == "unsupported":
            state.setdefault(it["key"], {"status": "unsupported", "reason": it["reason"]})
            continue
        if it["kind"] in ("gb", "hb", "db", "tt"):
            if it["year"] in std_index.get(code_key(it["code"], it["num"]), set()):
                state.setdefault(it["key"], {"status": "skipped_existing", "reason": "本地已有同年份文件"})
                continue
        elif it["kind"] == "law":
            if law_already_exists(it, law_index):
                state.setdefault(it["key"], {"status": "skipped_existing", "reason": "本地已有同版本文件"})
                continue
        elif it["kind"] == "mem":
            if norm_title(it["name"]) in law_index:
                state.setdefault(it["key"], {"status": "skipped_existing", "reason": "本地已有同名规章"})
                continue

        prev = state.get(it["key"], {})
        if prev.get("status") in TERMINAL_OK:
            if prev.get("file") and Path(prev["file"]).exists():
                continue
        if prev.get("status") in TERMINAL_SKIP and not args.retry:
            continue
        if prev.get("status") in ("failed", "blocked") and not (args.retry or args.retry_failed):
            continue
        todo.append(it)

    by_lane = defaultdict(list)
    for it in todo:
        lane = "laws" if it["kind"] == "law" else it["kind"]
        by_lane[lane].append(it)

    print(f"清单条目 {len(items)}，待处理 {len(todo)}")
    for lane in lanes:
        print(f"  {lane}: {len(by_lane.get(lane, []))} 条")
    if args.dry_run:
        return

    stats = defaultdict(int)
    write_lock = threading.Lock()
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"开始时间 {started}")

    def record(it, status, reason, path):
        with write_lock:
            state[it["key"]] = {"status": status, "reason": reason,
                                "file": str(path) if path else "",
                                "time": datetime.now().isoformat(timespec="seconds")}
            stats[status] += 1
            append_report([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), it["sheet"], it["name"],
                           status, reason, str(path) if path else ""])
            save_state(state)

    async def run_laws(law_items):
        dl = LawDownloader()
        await dl.start()
        try:
            for idx, it in enumerate(law_items, 1):
                print(f"[laws {idx}/{len(law_items)}] {it['name']}", flush=True)
                try:
                    status, reason, path = await dl.fetch_one(it, LAW_DIR)
                except Exception as e:
                    status, reason, path = "failed", f"{type(e).__name__}: {e}", None
                record(it, status, reason, path)
        finally:
            await dl.close()

    async def run_other(lane, lane_items):
        if lane == "hb" and args.concurrent > 1:
            workers = min(5, max(1, args.concurrent))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(download_hbdb, "hb", it): it for it in lane_items}
                done = 0
                for fut in as_completed(futures):
                    it = futures[fut]
                    done += 1
                    try:
                        status, reason, path = fut.result()
                    except Exception as e:
                        status, reason, path = "failed", f"{type(e).__name__}: {e}", None
                    print(f"[{lane} {done}/{len(lane_items)}] {status} {it['name']}", flush=True)
                    record(it, status, reason, path)
            return

        for idx, it in enumerate(lane_items, 1):
            print(f"[{lane} {idx}/{len(lane_items)}] {it['name']}", flush=True)
            try:
                if it["kind"] == "gb":
                    status, reason, path = download_gb(it)
                elif it["kind"] == "mem":
                    status, reason, path = download_mem(it)
                elif it["kind"] == "db":
                    status, reason, path = download_hbdb("db", it)
                else:
                    status, reason, path = download_tt(it)
            except Exception as e:
                status, reason, path = "failed", f"{type(e).__name__}: {e}", None
            record(it, status, reason, path)
            await asyncio.sleep(args.delay)

    for lane in lanes:
        lane_items = by_lane.get(lane, [])
        if args.limit:
            lane_items = lane_items[:args.limit]
        if not lane_items:
            continue
        if lane == "laws":
            asyncio.run(run_laws(lane_items))
        else:
            asyncio.run(run_other(lane, lane_items))

    print("\n完成统计:")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    print(f"报告: {REPORT_FILE}")
    print(f"状态: {STATE_FILE}")


if __name__ == "__main__":
    main()
