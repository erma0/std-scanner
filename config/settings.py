"""
项目配置常量 — API 端点 / 输出路径 / 行业代码映射 / 浏览器配置
高性能版本：优化的 HTTP 连接池 + 持久化的验证码客户端
"""
import httpx
import threading
import atexit
from pathlib import Path

from config.paths import BASE_DIR

# ==================== 输出路径 ====================
OUTPUT_DIR = Path.home() / "Downloads" / "安全标准"
REPORT_FILE = BASE_DIR / "safety_report.md"

# 输出目录缓存（避免高频下载时重复读磁盘）
_output_dir_cache = None
_output_dir_cache_ts = 0.0
_OUTPUT_DIR_CACHE_TTL = 5.0


def get_output_dir() -> Path:
    """获取当前配置的输出目录（带短时缓存）。
    
    优先从 config.json 读取用户配置的 output_dir，
    如果未配置或读取失败则回退到 OUTPUT_DIR 常量（~/Downloads/安全标准）。
    """
    global _output_dir_cache, _output_dir_cache_ts
    import time as _time
    now = _time.monotonic()
    if _output_dir_cache is not None and (now - _output_dir_cache_ts) < _OUTPUT_DIR_CACHE_TTL:
        return _output_dir_cache

    try:
        from config.manager import load_config
        cfg_dir = load_config().get('download', {}).get('output_dir', '')
        if cfg_dir:
            result = Path(cfg_dir)
        else:
            result = OUTPUT_DIR
    except Exception:
        result = OUTPUT_DIR

    _output_dir_cache = result
    _output_dir_cache_ts = now
    return result

# ==================== 服务端配置 ====================
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000

# ==================== API 端点 ====================
# 国标目录查询 API（GET 请求，参数: searchText/ics/state/ISSUE_DATE/sortOrder/pageSize/pageNumber）
API_BASE = "https://std.samr.gov.cn/gb/search/gbQueryPage"
# 国标详情页 URL（std.samr.gov.cn → 目录查询页，仅展示标准信息，无下载）
DETAIL_URL = "https://std.samr.gov.cn/gb/search/gbDetailed"
# 全文公开系统（openstd.samr.gov.cn → 提供下载/在线阅读）
OPENSTD = "https://openstd.samr.gov.cn/bzgk"
# 国标下载基础 URL（验证码 + viewGb 走 /bzgk/std/ 路径）
GB_DOWNLOAD_BASE = "https://openstd.samr.gov.cn/bzgk/std"
# 预览/验证码下载基础 URL（与 GB_DOWNLOAD_BASE 相同）
CAPTCHA_BASE = GB_DOWNLOAD_BASE

# 标准检索网站接口（HTML 渲染）
SEARCH_PAGE = "https://std.samr.gov.cn/search/stdPage"
SEARCH_MAIN = "https://std.samr.gov.cn/search/std"

# 行业标准 API
HB_API_URL = "https://hbba.sacinfo.org.cn/stdQueryList"
DB_API_URL = "https://dbba.sacinfo.org.cn/stdQueryList"

# ==================== 分页/间隔 ====================
PAGE_SIZE = 50

def _read_delay_from_config() -> float:
    try:
        from config.manager import load_config
        return float(load_config().get('download', {}).get('delay', 3.0))
    except Exception:
        return 3.0

class _Delay:
    """动态读取用户配置的请求延迟，5秒缓存避免高频读磁盘"""
    __slots__ = ('_v', '_ts')

    def __init__(self):
        self._v = 3.0
        self._ts = 0.0

    def __float__(self):
        import time as _t
        now = _t.monotonic()
        if now - self._ts < 5.0:
            return self._v
        self._v = _read_delay_from_config()
        self._ts = now
        return self._v

    def __int__(self): return int(float(self))
    def __index__(self): return int(float(self))
    def __neg__(self): return -float(self)
    def __bool__(self): return True
    def __add__(self, o): return float(self) + float(o)
    def __radd__(self, o): return float(o) + float(self)
    def __sub__(self, o): return float(self) - float(o)
    def __rsub__(self, o): return float(o) - float(self)
    def __mul__(self, o): return float(self) * float(o)
    def __rmul__(self, o): return float(o) * float(self)
    def __truediv__(self, o): return float(self) / float(o)
    def __rtruediv__(self, o): return float(o) / float(self)
    def __lt__(self, o): return float(self) < float(o)
    def __le__(self, o): return float(self) <= float(o)
    def __eq__(self, o): return float(self) == float(o)
    def __gt__(self, o): return float(self) > float(o)
    def __ge__(self, o): return float(self) >= float(o)
    def __str__(self): return str(float(self))
    def __repr__(self): return str(float(self))

DELAY = _Delay()

# ==================== 浏览器 ====================
BROWSER_CHANNELS = ['chrome', 'msedge']

# ==================== HTTP 客户端配置 ====================
_HTTP_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=50,
    keepalive_expiry=30.0,
)
_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_HTTP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

# ==================== 主 HTTP 客户端（连接池复用 + 自动重连）====================
# httpx.Transport 的 retries 参数控制连接级重试：
# 当 keepalive 连接被服务端关闭时，自动重建连接重试，避免 RemoteProtocolError
try:
    _transport = httpx.HTTPTransport(retries=3)
    http_client = httpx.Client(
        transport=_transport,
        headers=_HTTP_HEADERS,
        timeout=_HTTP_TIMEOUT,
        follow_redirects=True,
        limits=_HTTP_LIMITS,
        http2=True,
    )
except Exception:
    try:
        _transport = httpx.HTTPTransport(retries=3)
        http_client = httpx.Client(
            transport=_transport,
            headers=_HTTP_HEADERS,
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            limits=_HTTP_LIMITS,
        )
    except Exception:
        http_client = httpx.Client(
            headers=_HTTP_HEADERS,
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            limits=_HTTP_LIMITS,
        )

# ==================== 验证码下载客户端（持久化连接 + Cookie 隔离）====================
_captcha_clients: dict[str, httpx.Client] = {}
_captcha_lock = threading.Lock()


def get_captcha_client(site_type: str = 'gb') -> httpx.Client:
    """获取指定类型的验证码下载客户端（持久化 + 线程安全）
    
    Args:
        site_type: 'gb'（国标）| 'hb'（行标）| 'db'（地标）
    """
    with _captcha_lock:
        if site_type not in _captcha_clients:
            base_url = CAPTCHA_BASE if site_type == 'gb' else \
                       f'https://{site_type}ba.sacinfo.org.cn'
            client = httpx.Client(
                headers={
                    **_HTTP_HEADERS,
                    'Referer': f'{base_url}/showGb?type=download',
                },
                timeout=_HTTP_TIMEOUT,
                follow_redirects=True,
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                ),
                cookies=None,  # 独立的 Cookie
            )
            _captcha_clients[site_type] = client
        return _captcha_clients[site_type]


def close_captcha_clients():
    """关闭所有验证码客户端（程序退出时调用，幂等安全）"""
    with _captcha_lock:
        for client in _captcha_clients.values():
            try:
                if not client.is_closed:
                    client.close()
            except Exception:
                pass
        _captcha_clients.clear()


# 注册退出回调
atexit.register(close_captcha_clients)


def _close_http_client():
    """关闭主 HTTP 客户端连接池（程序退出时调用，幂等安全）"""
    try:
        if not http_client.is_closed:
            http_client.close()
    except Exception:
        pass


atexit.register(_close_http_client)

# ==================== 行业代码映射 ====================
HB_CODE_MAP = {
    'AQ': '安全生产', 'BB': '包装', 'CB': '船舶', 'CH': '测绘',
    'CJ': '城镇建设', 'CY': '新闻出版', 'DA': '档案', 'DB': '地震',
    'DL': '电力', 'DY': '电影', 'DZ': '地质矿产', 'EJ': '核工业',
    'FZ': '纺织', 'GA': '公共安全', 'GC': '国家物资储备', 'GF': '国防工业',
    'GH': '供销合作', 'GM': '国密', 'GY': '广播电视和网络视听', 'HB': '航空',
    'HG': '化工', 'HJ': '环境保护', 'HS': '海关', 'HY': '海洋',
    'JB': '机械', 'JC': '建材', 'JG': '建筑工程', 'JR': '金融',
    'JS': '机关事务', 'JT': '交通', 'JY': '教育', 'KA': '矿山安全',
    'LB': '旅游', 'LD': '劳动和劳动安全', 'LS': '粮食', 'LY': '林业',
    'MH': '民用航空', 'MR': '市场监管', 'MT': '煤炭', 'MZ': '民政',
    'NB': '能源', 'NY': '农业', 'QB': '轻工', 'QC': '汽车',
    'QJ': '航天', 'QX': '气象', 'RB': '认证认可', 'RF': '人民防空',
    'SB': '国内贸易', 'SC': '水产', 'SF': '司法', 'SH': '石油化工',
    'SJ': '电子', 'SL': '水利', 'SN': '出入境检验检疫', 'SW': '税务',
    'SY': '石油天然气', 'TB': '铁路', 'TD': '土地管理', 'TY': '体育',
    'WB': '物资管理', 'WH': '文化', 'WJ': '兵工民品', 'WM': '外经贸',
    'WS': '卫生', 'WW': '文物保护', 'XB': '稀土', 'XF': '消防救援',
    'YB': '黑色冶金', 'YC': '烟草', 'YD': '通信',
    'YJ': '减灾救灾与综合性应急管理', 'YS': '有色金属', 'YY': '医药',
    'YZ': '邮政', 'ZY': '中医药',
}

HB_SAFETY_CODES = [
    # 安全生产核心
    'AQ',   # 安全生产
    'KA',   # 矿山安全
    'XF',   # 消防救援
    'GA',   # 公共安全
    'LD',   # 劳动和劳动安全
    'YJ',   # 减灾救灾与综合性应急管理
    # 高危行业
    'HG',   # 化工
    'SH',   # 石油化工
    'SY',   # 石油天然气
    'MT',   # 煤炭
    'EJ',   # 核工业
    # 能源电力
    'DL',   # 电力
    'NB',   # 能源
    # 建筑交通
    'JG',   # 建筑工程
    'JT',   # 交通
    # 机械制造
    'JB',   # 机械
]

# ==================== 地方标准省份代码 ====================
DB_PROVINCE_MAP = {
    'beijing': '110000', 'tianjin': '120000', 'hebei': '130000',
    'shanxi': '140000', 'neimenggu': '150000', 'liaoning': '210000',
    'jilin': '220000', 'heilongjiang': '230000', 'shanghai': '310000',
    'jiangsu': '320000', 'zhejiang': '330000', 'anhui': '340000',
    'fujian': '350000', 'jiangxi': '360000', 'shandong': '370000',
    'henan': '410000', 'hubei': '420000', 'hunan': '430000',
    'guangdong': '440000', 'guangxi': '450000', 'hainan': '460000',
    'chongqing': '500000', 'sichuan': '510000', 'guizhou': '520000',
    'yunnan': '530000', 'xizang': '540000', 'shaanxi': '610000',
    'gansu': '620000', 'qinghai': '630000', 'ningxia': '640000',
    'xinjiang': '650000',
}


def _resolve_hb_industry(code_or_name: str) -> str:
    """根据行业代码返回中文名称"""
    if not code_or_name:
        return ''
    upper = code_or_name.upper().strip()
    if upper in HB_CODE_MAP:
        return HB_CODE_MAP[upper]
    return code_or_name
