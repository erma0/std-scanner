"""
安全关键词匹配模块
关键词组存储在 config.json 的 keyword_groups 中。
每个组包含: keywords(匹配词), excludes(排除词), industries(行业筛选), provinces(省份筛选)
"""
import threading
import time
import re
import logging
from typing import Dict, Set, Optional

from config.settings import HB_SAFETY_CODES
from config.manager import load_config, save_config

_log = logging.getLogger('std_scraper')

# ==================== 预设 "安全生产" 组 ====================
_PRESET_KEYWORDS = [
    # 安全通用（根词匹配：事故/隐患/安全风险为新增）
    '安全', '防护', '隐患', '安全风险', '事故',
    # 消防防爆
    '消防', '防火', '灭火', '火灾', '防爆', '爆炸', '阻燃',
    '排烟', '防烟', '消火栓',
    # 危化品
    '危险', '有害', '有毒', '腐蚀', '辐射',
    '剧毒', '易燃', '易爆',
    '泄漏', '中毒', '窒息', '灼伤',
    '危化品', '氧化剂', '过氧化物',
    # 特种设备
    '锅炉', '压力容器', '压力管道', '特种设备',
    '起重', '电梯', '气瓶', '储罐',
    '高压', '压缩', '叉车',
    '压力表', '爆破片', '场内车辆',
    '紧急切断',
    # 职业卫生
    '职业病', '粉尘', '防尘', '噪声', '振动', '毒物',
    '防毒面具', '护听器', '职业卫生', '职业暴露',
    '高温作业', '低温作业',
    # 应急管理
    '应急', '救援', '逃生', '疏散', '报警',
    '突发事件',
    # 电气电力
    '触电', '绝缘', '接地', '漏电',
    '变电', '配电', '防雷', '静电',
    '带电作业', '临时用电',
    # 建筑施工
    '建筑施工', '脚手架', '基坑', '塔吊',
    '高处作业', '施工升降机',
    # 工艺作业
    '焊接', '切割', '铸造', '锻造', '热处理',
    '动火', '盲板', '试压', '联锁', '吊装',
    '受限空间', '有限空间', '密闭空间',
    # 氨制冷 / 冷链
    '制冷', '液氨', '氨气', '冷库', '冷链', '冷冻',
    # 行业领域
    '化工', '石油', '燃气', '天然气',
    '煤矿', '矿山', '冶金', '冶炼',
    '储运',
    # 爆炸物
    '民爆', '烟花爆竹',
    # 场所风险（仅保留有安全属性的场所描述词）
    '人员密集', '公众聚集', '地下建筑', '地下空间',
]

_PRESET_EXCLUDES = ['信息安全', '网络安全', '数据安全', '食品安全', '农产品', '饲料']

_PRESET_INDUSTRIES = list(HB_SAFETY_CODES)
_PRESET_PROVINCES = ['江苏省']

PRESET_GROUP = {
    'keywords': list(_PRESET_KEYWORDS),
    'excludes': list(_PRESET_EXCLUDES),
    'industries': list(_PRESET_INDUSTRIES),
    'provinces': list(_PRESET_PROVINCES),
}

# ==================== 线程本地存储 ====================
_tls = threading.local()

# 缓存：避免每次 is_safety 调用都读磁盘
_groups_cache: Optional[Dict] = None
_groups_cache_time = 0
_groups_cache_ttl = 60  # 延长至 1 分钟缓存
_lock = threading.Lock()

# 正则匹配器缓存：{group_name: SafetyMatcher}
_matcher_cache: Dict[str, 'SafetyMatcher'] = {}
_matcher_cache_lock = threading.Lock()


def _empty_group() -> dict:
    """新建组的空模板"""
    return {'keywords': [], 'excludes': [], 'industries': [], 'provinces': []}


class SafetyMatcher:
    """高性能关键词匹配器（使用编译好的正则表达式）"""
    __slots__ = ('_excludes', '_exclude_pattern', '_keywords_pattern')

    def __init__(self, keywords, excludes):
        self._excludes = set(ex for ex in excludes if ex)
        
        # 收集所有关键词
        all_keywords: Set[str] = set(kw for kw in keywords if kw)
        
        # 编译正则表达式（按长度降序排序，避免短词匹配覆盖长词）
        self._exclude_pattern = self._compile_pattern(sorted(self._excludes, key=lambda x: -len(x))) if self._excludes else None
        self._keywords_pattern = self._compile_pattern(sorted(all_keywords, key=lambda x: -len(x))) if all_keywords else None

    @staticmethod
    def _compile_pattern(keywords):
        """编译关键词正则表达式"""
        if not keywords:
            return None
        pattern = '|'.join(re.escape(kw) for kw in keywords)
        return re.compile(pattern, flags=re.IGNORECASE)

    def is_safety(self, text: str, std_type: str = None) -> bool:
        """高性能匹配 - 所有关键词对所有类型标准都通用"""
        if not text:
            return False
        # ① 先检查排除词
        if self._exclude_pattern and self._exclude_pattern.search(text):
            return False

        # ② 检查所有关键词（不管 std_type 是什么）
        if self._keywords_pattern and self._keywords_pattern.search(text):
            return True
        
        return False


def _load_groups_from_config() -> dict:
    """从 config.json 加载所有关键词组（带 60 秒缓存 + 锁保护）"""
    global _groups_cache, _groups_cache_time
    now = time.time()
    if _groups_cache is not None and (now - _groups_cache_time) < _groups_cache_ttl:
        return _groups_cache

    with _lock:
        if _groups_cache is not None and (now - _groups_cache_time) < _groups_cache_ttl:
            return _groups_cache

        config = load_config()
        groups = config.get('keyword_groups', {})

        # 默认组 fallback：不存在或关键词为空时使用预设
        if not groups or '安全生产' not in groups:
            groups['安全生产'] = dict(PRESET_GROUP)
        else:
            grp = groups['安全生产']
            kws = grp.get('keywords', [])
            if not kws:
                groups['安全生产'] = dict(PRESET_GROUP)

        _groups_cache = groups
        _groups_cache_time = now

        # 清除旧的 matcher 缓存（当组配置变化时）
        with _matcher_cache_lock:
            _matcher_cache.clear()

        return groups


def invalidate_groups_cache():
    """清除关键词组缓存（在 save_groups/reset 后调用）"""
    global _groups_cache, _groups_cache_time
    with _lock:
        _groups_cache = None
        _groups_cache_time = 0
    with _matcher_cache_lock:
        _matcher_cache.clear()


def set_active_group(name: str = '安全生产'):
    """设置当前线程活跃的关键词组"""
    _tls.active_group = name


def get_active_group() -> str:
    return getattr(_tls, 'active_group', '安全生产')


def _get_active_config() -> dict:
    """获取当前活跃组的完整配置"""
    groups = _load_groups_from_config()
    name = getattr(_tls, 'active_group', '安全生产')
    return groups.get(name, groups.get('安全生产', PRESET_GROUP))


def _get_matcher(group_name: str) -> SafetyMatcher:
    """获取或创建匹配器（带缓存）。
    
    注意：_load_groups_from_config 在 _matcher_cache_lock 外调用，
    避免与 _load_groups_from_config 内部 _lock→_matcher_cache_lock 形成 ABBA 死锁。
    """
    with _matcher_cache_lock:
        if group_name in _matcher_cache:
            return _matcher_cache[group_name]

    # 锁外加载配置（_load_groups_from_config 会获取 _lock）
    groups = _load_groups_from_config()
    cfg = groups.get(group_name, groups.get('安全生产', PRESET_GROUP))
    matcher = SafetyMatcher(
        cfg.get('keywords', []),
        cfg.get('excludes', [])
    )

    with _matcher_cache_lock:
        _matcher_cache.setdefault(group_name, matcher)
        return _matcher_cache[group_name]


# ==================== 关键词组 CRUD ====================

def get_all_groups() -> dict:
    return _load_groups_from_config()


def save_groups(groups: dict):
    config = load_config()
    config['keyword_groups'] = groups
    save_config(config)
    invalidate_groups_cache()


def import_to_group(name: str, text: str) -> dict:
    lines = [l.strip() for l in text.split('\n') if l.strip() and not l.startswith('#')]
    groups = get_all_groups()
    if name not in groups:
        groups[name] = _empty_group()
    grp = groups[name]
    existing = set(grp.get('keywords', []))
    added = 0
    for kw in lines:
        if kw not in existing:
            existing.add(kw)
            added += 1
    grp['keywords'] = list(existing)
    groups[name] = grp
    save_groups(groups)
    return {'group': name, 'count': len(grp['keywords']), 'added': added}


def delete_group(name: str) -> bool:
    if name == '安全生产':
        return False
    groups = get_all_groups()
    if name in groups:
        del groups[name]
        save_groups(groups)
        return True
    return False


def reset_to_default():
    save_groups({'安全生产': dict(PRESET_GROUP)})
    invalidate_groups_cache()


# ==================== 关键词匹配 ====================

def is_safety(text: str, std_type: str = None) -> bool:
    """判断标准名称是否安全相关。
    高性能版本：使用编译好的正则表达式。
    匹配顺序：①排除词 → ②匹配词
    所有关键词对 GB/HB/DB 所有类型标准都通用。
    """
    if not text:
        return False
    t = text.replace('<sacinfo>', '').replace('</sacinfo>', '')
    group_name = getattr(_tls, 'active_group', '安全生产')
    
    try:
        matcher = _get_matcher(group_name)
        return matcher.is_safety(t, std_type)
    except Exception:
        # 回退到简单匹配（防止正则编译失败）
        cfg = _get_active_config()
        for ex in cfg.get('excludes', []):
            if ex and ex in t:
                return False
        for kw in cfg.get('keywords', []):
            if kw and kw in t:
                return True
        return False


def is_aq_yj(code: str) -> bool:
    return code and (code.startswith('AQ') or code.startswith('YJ'))


def clean_name(name: str) -> str:
    return (name or '').replace('<sacinfo>', '').replace('</sacinfo>', '')


def load_keywords(filepath=None):
    """加载关键词列表。

    如果指定了 filepath，则从文件逐行读取关键词；
    否则从 config.json 的安全生产组读取。
    """
    if filepath:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip() and not line.startswith('#')]
        except Exception as e:
            _log.warning(f"从文件加载关键词失败 ({filepath}): {e}")

    groups = _load_groups_from_config()
    grp = groups.get('安全生产', PRESET_GROUP)
    return grp.get('keywords', [])
