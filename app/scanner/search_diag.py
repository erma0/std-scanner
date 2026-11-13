"""scanner.search_diag — 批量搜索诊断

记录每个查询的结果去向（站点返回 / 意图匹配 / 候选新增 / 重复 / 本地已有 /
不可下载 / 废止 / 异常），用于：
  1. 输出可读日志，直接回答"为什么这次查询没有新增结果"；
  2. 作为任务字段推送给前端，展示搜索诊断面板；
  3. 汇总批量搜索的整体命中率，便于判断站点是否限流或检索词质量。

注意：这里的"候选"指加入待下载列表的条目，不等于最终下载成功数。
"""

_MAX_ZERO_SAMPLES = 50


class SearchBatchDiag:
    """批量搜索诊断记录器（单任务实例）。"""

    def __init__(self, total_queries):
        self.total = total_queries
        self.done = 0
        self.hit_queries = 0        # 站点有返回的查询数
        self.empty_queries = 0      # 站点 0 条结果的查询数
        self.error_queries = 0      # 请求/解析异常的查询数
        self.raw_total = 0          # 站点原始返回条数合计
        self.matched_total = 0      # 意图过滤后条数合计
        self.added_total = 0        # 加入候选列表条数合计
        self.duplicate_total = 0    # 同批次重复条数
        self.existing_total = 0     # 本地已有条数
        self.unavailable_total = 0  # 不可下载（计划/付费/未知类型）条数
        self.obsolete_total = 0     # 废止版本跳过条数
        self.consecutive_empty = 0  # 连续"站点无结果"查询数
        self.warning = ''           # 连续无结果告警（可能被限流）
        self.zero_samples = []      # 最近 0 新增的查询及原因
        self.last = {}              # 最近一次查询结果

    def record(self, query, raw=0, matched=0, added=0, duplicate=0,
               existing=0, unavailable=0, obsolete=0, site_total=0,
               error='', sample_codes=None):
        """记录一次查询，返回本次查询的可读原因（有新增时为空字符串）。"""
        self.done += 1
        self.raw_total += raw
        self.matched_total += matched
        self.added_total += added
        self.duplicate_total += duplicate
        self.existing_total += existing
        self.unavailable_total += unavailable
        self.obsolete_total += obsolete

        if error:
            self.error_queries += 1
            self.consecutive_empty = 0
        elif raw > 0:
            self.hit_queries += 1
            self.consecutive_empty = 0
        else:
            self.empty_queries += 1
            self.consecutive_empty += 1
            # 连续无结果通常是站点限流/风控或检索词均未收录，给出一次醒目告警
            if self.consecutive_empty in (10, 25) or self.consecutive_empty % 50 == 0:
                self.warning = (f'连续 {self.consecutive_empty} 个查询站点无结果，'
                                f'可能被限流/风控，或检索词均未收录')

        reason = self._reason(raw, matched, added, duplicate, existing,
                              unavailable, obsolete, site_total, error, sample_codes)
        self.last = {
            'query': query, 'raw': raw, 'matched': matched, 'added': added,
            'reason': reason, 'error': bool(error),
        }
        if not added:
            if len(self.zero_samples) >= _MAX_ZERO_SAMPLES:
                self.zero_samples.pop(0)
            self.zero_samples.append({'query': query, 'reason': reason})
        return reason

    @staticmethod
    def _reason(raw, matched, added, duplicate, existing, unavailable,
                obsolete, site_total, error, sample_codes):
        if added:
            return ''
        if error:
            return f'搜索异常：{error}'
        if raw == 0:
            if site_total:
                return f'站点显示 {site_total} 条但页面未解析出结果（疑似页面改版或风控）'
            return '站点无结果'
        if matched == 0:
            hint = '、'.join(c for c in (sample_codes or [])[:3] if c)
            return f'站点返回 {raw} 条但编号不匹配' + (f'（如 {hint}）' if hint else '')
        parts = []
        if duplicate:
            parts.append(f'重复 {duplicate}')
        if existing:
            parts.append(f'本地已有 {existing}')
        if unavailable:
            parts.append(f'不可下载 {unavailable}')
        if obsolete:
            parts.append(f'废止 {obsolete}')
        return '、'.join(parts) if parts else f'命中 {matched} 条但未新增'

    def brief(self):
        """简短概览，适合任务卡片消息。"""
        return (f'查询 {self.done}/{self.total}：'
                f'有结果 {self.hit_queries} / 空结果 {self.empty_queries} / 异常 {self.error_queries}')

    def overview(self):
        """完整概览，适合日志与详情面板。"""
        parts = [self.brief(), f'站点原始 {self.raw_total} 条',
                 f'意图匹配 {self.matched_total} 条', f'候选 {self.added_total} 条']
        if any((self.duplicate_total, self.existing_total,
                self.unavailable_total, self.obsolete_total)):
            skip = []
            if self.duplicate_total:
                skip.append(f'重复 {self.duplicate_total}')
            if self.existing_total:
                skip.append(f'本地已有 {self.existing_total}')
            if self.unavailable_total:
                skip.append(f'不可下载 {self.unavailable_total}')
            if self.obsolete_total:
                skip.append(f'废止 {self.obsolete_total}')
            parts[-1] += '（' + '、'.join(skip) + '）'
        return '，'.join(parts)

    def to_dict(self):
        return {
            'total': self.total, 'done': self.done,
            'hit_queries': self.hit_queries,
            'empty_queries': self.empty_queries,
            'error_queries': self.error_queries,
            'raw_total': self.raw_total,
            'matched_total': self.matched_total,
            'added_total': self.added_total,
            'duplicate_total': self.duplicate_total,
            'existing_total': self.existing_total,
            'unavailable_total': self.unavailable_total,
            'obsolete_total': self.obsolete_total,
            'consecutive_empty': self.consecutive_empty,
            'warning': self.warning,
            'zero_samples': list(self.zero_samples),
            'last': dict(self.last),
        }
