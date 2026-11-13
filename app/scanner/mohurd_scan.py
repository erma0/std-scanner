"""scanner.mohurd_scan — 工程建设国家标准（住建部公告）检索与下载

工程建设国家标准（GB 50xxx–55xxx）不在 std.samr.gov.cn 检索体系内，由
住房和城乡建设部在"标准公告"中发布，公告页的附件即标准全文 PDF。

流程：
  1. jpaas-jsearch 站内检索（用紧凑编号，如 GB50016-2014）
  2. 在结果中挑选"发布国家标准/行业标准"公告（跳过英文版、征求意见、答复）
  3. 打开公告页，用正文"编号为GB..."确认编号一致
  4. 取附件直链 document/download，流式写入磁盘（PDF 常有几十 MB）

注意：
  - "局部修订条文"公告的附件可能只含修订条款，不代表全本；
  - 早期标准可能没有附件，此时标记为无全文（需购买正式出版物）。
"""

import logging
import re
import urllib.parse

import httpx

from app.helpers import normalize_code
from config.settings import (
    MOHURD_BASE, MOHURD_SEARCH_API, MOHURD_SEARCH_SERVICE_ID,
    MOHURD_SEARCH_CATE_ID, http_client,
)

_log = logging.getLogger('std_scraper')

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

_SEARCH_PAGE_SIZE = 20
_MAX_ANNOUNCEMENTS = 5

_ART_ID_RE = re.compile(r'art_([0-9a-f_]+)\.html', re.I)
_CODE_RE = re.compile(r'[A-Z]{1,5}(?:/T)?\s*\d{1,5}(?:\.\d+)?\s*[-—–]\s*\d{2,4}')
_ISSUE_CODE_RE = re.compile(
    r'编号为\s*([A-Z]{1,5}(?:/T)?\s*\d{1,5}(?:\.\d+)?\s*[-—–]\s*\d{2,4})')
_ATTACH_RE = re.compile(
    r'href="([^"]*document/download[^"]*)"[^>]*>([^<]*)</a>', re.S)
_PUBLISH_RE = re.compile(r'发布时间：\s*(\d{4}-\d{2}-\d{2})')
_NAME_RE = re.compile(r'《([^》]+)》')

_SKIP_TITLE_WORDS = ('英文版', '征求意见', '答复', '声明', '公示', '通知')


def _compact(code):
    """去掉空白并统一大小写，用于编号比对。"""
    return re.sub(r'\s+', '', normalize_code(str(code or ''))).upper()


def _with_space(code):
    """GB51043-2014 → GB 51043-2014，保持与检索结果一致的编号格式。"""
    norm = normalize_code(str(code or '')).strip()
    m = re.match(r'^([A-Za-z]{1,6}(?:/[A-Za-z]{1,4})?)\s*(\d.*)$', norm)
    return f'{m.group(1)} {m.group(2)}' if m else norm


def _search_announcements(code):
    """调用住建部站内检索，返回 [{title, url}]。"""
    params = {
        'serviceId': MOHURD_SEARCH_SERVICE_ID,
        'websiteid': '',
        'cateid': MOHURD_SEARCH_CATE_ID,
        'q': _compact(code),
        'pg': str(_SEARCH_PAGE_SIZE),
        'p': '1',
        'pos': '',
        'sortType': '',
    }
    headers = {
        'User-Agent': _UA,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f'{MOHURD_BASE}/',
    }
    resp = http_client.get(MOHURD_SEARCH_API, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    result = ((payload.get('data') or {}).get('searchResult') or {}).get('result') or []

    items, seen = [], set()
    for block in result:
        m = re.search(r'href="(https?://[^"]+)"', block)
        if not m or m.group(1) in seen:
            continue
        url = m.group(1)
        seen.add(url)
        title_match = re.search(r'data-title="([^"]+)"', block)
        if not title_match:
            title_match = re.search(r'class="textTitle"[^>]*>(.*?)</a>', block, re.S)
            title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip() if title_match else ''
        else:
            title = title_match.group(1)
        items.append({'title': title, 'url': url})
    return items


def _is_announcement(item):
    """只保留文件库里的标准公告页。"""
    return '/gongkai/zc/wjk/art/' in item.get('url', '')


def _candidate_score(title):
    """公告候选优先级：正式发布 > 局部修订 > 其他。"""
    if any(w in title for w in _SKIP_TITLE_WORDS):
        return -100
    score = 0
    if '发布' in title:
        score += 2
    if '国家标准' in title or '行业标准' in title:
        score += 2
    if '局部修订' in title:
        score -= 5
    return score


def _extract_name(title):
    m = _NAME_RE.search(title or '')
    return m.group(1).strip() if m else ''


def _fetch_announcement(url, title):
    """抓取公告页，返回编号/附件/发布时间；失败返回 None。"""
    try:
        resp = http_client.get(url, headers={'User-Agent': _UA}, timeout=30)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        _log.warning(f"[MOHURD] 公告页请求失败 {url}: {type(e).__name__}: {e}")
        return None

    codes = sorted({ _with_space(c) for c in _ISSUE_CODE_RE.findall(html) })
    if not codes:
        codes = sorted({ _with_space(c) for c in _CODE_RE.findall(html) })

    attachments = []
    for href, label in _ATTACH_RE.findall(html):
        href = urllib.parse.urljoin(url, href.replace('&amp;', '&'))
        attachments.append({'url': href, 'label': re.sub(r'<[^>]+>', '', label).strip()})

    pub = _PUBLISH_RE.search(html)
    return {
        'codes': codes,
        'attachments': attachments,
        'publishDate': pub.group(1) if pub else '',
        'name': _extract_name(title),
    }


def find_mohurd_standard(code):
    """按标准编号查找住建部公告，返回可入库的候选 dict；找不到返回 None。"""
    target = _compact(code)
    if not target:
        return None

    try:
        results = _search_announcements(code)
    except Exception as e:
        raise RuntimeError(f'住建部检索失败: {type(e).__name__}: {e}') from e

    candidates = [r for r in results if _is_announcement(r)]
    candidates.sort(key=lambda r: _candidate_score(r.get('title') or ''), reverse=True)

    # 收集所有编号匹配的公告，优先选带附件、且非"局部修订"的
    matches = []
    for cand in candidates[:_MAX_ANNOUNCEMENTS]:
        if _candidate_score(cand.get('title') or '') <= -100:
            continue
        info = _fetch_announcement(cand['url'], cand['title'])
        if not info:
            continue
        if target not in {_compact(c) for c in info['codes']}:
            continue
        matches.append((cand, info))
        if info['attachments'] and '局部修订' not in cand['title']:
            break  # 已找到正式发布且有附件的公告，无需继续
    if not matches:
        return None

    matches.sort(key=lambda m: (bool(m[1]['attachments']),
                                '局部修订' not in m[0]['title']), reverse=True)
    cand, info = matches[0]
    revision = '局部修订' in cand['title']
    attachment = info['attachments'][0] if info['attachments'] else None
    art = _ART_ID_RE.search(cand['url'])
    item = {
        'code': _with_space(code),
        'name': info['name'] or (attachment['label'] if attachment else ''),
        'tid': 'BV_MOHURD',
        'pid': art.group(1) if art else cand['url'],
        'status': '',
        'publishDate': info['publishDate'],
        'detailUrl': cand['url'],
        'pdf_url': attachment['url'] if attachment else '',
        'downloadable': True,
        'download_msg': '住建部公告附件' if attachment else '公告未提供附件',
    }
    if revision:
        item['download_msg'] += '（局部修订，可能仅含修订条文）'
    return item


class _Aborted(Exception):
    """下载被暂停/删除中断。"""


def download_mohurd_pdf(pdf_url, filepath, referer='', abort_check=None):
    """流式下载住建部公告附件 PDF，返回 (ok, reason)。

    PDF 常有几十 MB，不能像小文件一样整块读入内存；先写 .part 再改名，
    避免中断后留下半个文件被去重逻辑误判为已存在。
    """
    if abort_check and abort_check():
        return False, 'aborted'
    tmp = filepath.with_name(filepath.name + '.part')
    headers = {'User-Agent': _UA}
    if referer:
        headers['Referer'] = referer
    try:
        with http_client.stream('GET', pdf_url, headers=headers,
                                timeout=httpx.Timeout(300.0, connect=30.0)) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get('content-type', '')
            first = b''
            with open(tmp, 'wb') as f:
                for chunk in resp.iter_bytes(256 * 1024):
                    if abort_check and abort_check():
                        raise _Aborted()
                    if not first:
                        first = chunk[:5]
                    f.write(chunk)
        if first != b'%PDF-':
            tmp.unlink(missing_ok=True)
            return False, f'响应非PDF(ct={content_type})'
        if tmp.stat().st_size < 1024:
            tmp.unlink(missing_ok=True)
            return False, '文件过小'
        tmp.replace(filepath)
        return True, ''
    except _Aborted:
        tmp.unlink(missing_ok=True)
        return False, 'aborted'
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f'{type(e).__name__}: {e}'
