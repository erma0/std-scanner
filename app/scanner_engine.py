"""
统一扫描引擎 — 消除 server.py 中三处扫描逻辑重复

无论来自定时扫描、联合扫描、还是任务重试，所有 GB/HB/DB 扫描→下载
流程都走同一个入口：run_scan_pipeline()

设计原则：
  - 单一职责：本模块只负责"编排"，不负责具体扫描/下载实现
  - 零重复：GB/HB/DB 三种类型共用同一个编排函数
  - 进度追踪：通过 task_manager 统一更新进度（可选）
"""

import asyncio
import time
import logging

_log = logging.getLogger('std_scraper')

# 运行时依赖 — 在 scan_gb/scan_hb/scan_db 中按需调用
from app.scanner import (
    scan_pages, download_phase,
    scan_hb_standards, download_hb_standards,
    scan_db_standards, download_db_standards,
    compare_snapshot,
    compute_download_stats,
)


async def run_scan_pipeline(
    scan_type: str,       # 'gb' | 'hb' | 'db'
    config: dict,         # { max_results, incr, keyword_group, industries?, provinces? }
    task_id: str = None,  # 可选：任务 ID，用于进度追踪
    task_manager=None,    # 可选：TaskManager 实例
    progress_base: int = 0,       # 进度起始百分比
    progress_per_scan: int = 40,  # 扫描阶段进度占比
    progress_per_download: int = 60,  # 下载阶段进度占比
) -> list:
    """
    统一的扫描→下载编排函数。

    调用方只需指定 scan_type 和 config，无需关心内部实现差异。

    Args:
        scan_type: 'gb' | 'hb' | 'db'
        config: 扫描配置字典
        task_id: 任务 ID（进度追踪用）
        task_manager: TaskManager 实例
        progress_base: 进度条起始位置
        progress_per_scan: 扫描阶段占用进度范围
        progress_per_download: 下载阶段占用进度范围

    Returns:
        扫描到的标准列表
    """
    max_results = config.get('max_results', 500)
    incr = config.get('incr', False)
    keyword_group = config.get('keyword_group', '安全生产')
    scan_only = config.get('scan_only', False)
    std_state = config.get('std_state', '现行')

    _log.info(f"[ENGINE] 开始 {scan_type.upper()} 扫描: max={max_results}, incr={incr}, group={keyword_group}, state={std_state}")

    type_label = {'gb': '国家标准', 'hb': '行业标准', 'db': '地方标准'}.get(scan_type, scan_type)

    # === 构建进度回调 ===
    async def _on_scan_progress(pct, msg):
        if task_manager and task_id:
            scaled = progress_base + max(1, int(pct * progress_per_scan / 100))
            task_manager.update(task_id, progress=scaled, scan_progress=pct,
                              message=msg, persist_std_items=False)

    async def _on_dl_progress(pct, msg):
        if task_manager and task_id:
            scaled = progress_base + progress_per_scan + max(1, int(pct * progress_per_download / 100))
            task_manager.update(task_id, progress=scaled, dl_progress=pct,
                              message=msg, persist_std_items=False)

    def _on_sync_scan_progress(pct, msg):
        """同步回调（用于 run_in_executor 中的 HB/DB 扫描）"""
        if task_manager and task_id:
            scaled = progress_base + max(1, int(pct * progress_per_scan / 100))
            task_manager.update(task_id, progress=scaled, scan_progress=pct,
                              message=msg, persist_std_items=False)

    _intermediate_counter = [0]

    def _on_intermediate(standards):
        """推送中间扫描结果（HB/DB 每页后调用，从线程内调用）"""
        if task_manager and task_id:
            _intermediate_counter[0] += 1
            # 每 3 次中间结果才全量持久化 std_items，减少 SQLite 写入
            persist = _intermediate_counter[0] % 3 == 0
            task_manager.update(task_id, std_items=list(standards),
                              stats={'scanned': len(standards)}, persist_std_items=persist)

    async def _on_scan_intermediate(standards):
        """推送中间扫描结果（GB async 上下文）"""
        if task_manager and task_id:
            _intermediate_counter[0] += 1
            persist = _intermediate_counter[0] % 3 == 0
            task_manager.update(task_id, std_items=list(standards),
                              stats={'scanned': len(standards)}, persist_std_items=persist)

    async def _check_pause():
        """等待暂停恢复（async 上下文），返回 False 表示应中止"""
        while task_manager and task_id:
            task = task_manager.get(task_id)
            if not task:
                return False
            status = task.get('status')
            if status == 'running':
                return True
            if status != 'paused':
                return False
            await asyncio.sleep(1)
        return True

    def _check_pause_sync():
        """等待暂停恢复（sync/线程上下文），返回 False 表示应中止"""
        while task_manager and task_id:
            task = task_manager.get(task_id)
            if not task:
                return False
            status = task.get('status')
            if status == 'running':
                return True
            if status != 'paused':
                return False
            time.sleep(1)
        return True

    # === 阶段1: 扫描 ===
    if task_manager and task_id:
        task_manager.update(task_id,
            progress=progress_base + 1,
            scan_progress=0,
            message=f"扫描中{type_label}...")

    standards = []

    if scan_type == 'gb':
        standards = await scan_pages(max_results, incr=incr, keyword_group=keyword_group,
                                     on_progress=_on_scan_progress, check_pause=_check_pause,
                                     on_intermediate=_on_scan_intermediate, state=std_state)

    elif scan_type == 'hb':
        industries = config.get('industries', None)
        loop = asyncio.get_running_loop()
        standards = await loop.run_in_executor(
            None, lambda: scan_hb_standards(
                industries=industries,
                max_results=max_results,
                incr=incr,
                keyword_group=keyword_group,
                on_progress=_on_sync_scan_progress,
                on_intermediate=_on_intermediate,
                check_pause=_check_pause_sync, status=std_state,
            )
        )

    elif scan_type == 'db':
        provinces = config.get('provinces', None)
        loop = asyncio.get_running_loop()
        standards = await loop.run_in_executor(
            None, lambda: scan_db_standards(
                provinces=provinces,
                max_results=max_results,
                incr=incr,
                keyword_group=keyword_group,
                on_progress=_on_sync_scan_progress,
                on_intermediate=_on_intermediate,
                check_pause=_check_pause_sync, status=std_state,
            )
        )

    if task_manager and task_id:
        task_manager.update(task_id,
            progress=progress_base + progress_per_scan,
            scan_progress=100,
            message=f"{type_label}扫描完成({len(standards)}条)",
            stats={'scanned': len(standards)},
            std_items=standards)

    _log.info(f"[ENGINE] {scan_type.upper()} 扫描完成: {len(standards)} 条")

    # 变更追踪：与上次快照对比，检测新增/状态变更
    changes = None
    try:
        changes = compare_snapshot(scan_type, standards)
        if not changes['new_scan']:
            if changes['added']:
                _log.info(f"[ENGINE] {scan_type.upper()} 新增标准: {len(changes['added'])} 条")
            if changes['changed']:
                for c in changes['changed']:
                    _log.info(f"[ENGINE] {scan_type.upper()} 状态变更: {c['code']} {c['name']} ({c['old_state']} → {c['new_state']})")
            if changes['removed']:
                _log.info(f"[ENGINE] {scan_type.upper()} 已删除标准: {len(changes['removed'])} 条")
            # 将变更结果存入任务，供前端展示
            if task_manager and task_id and (changes['added'] or changes['changed'] or changes['removed']):
                task_manager.update(task_id, changes=changes)
    except Exception as e:
        _log.debug(f"[ENGINE] 变更追踪异常（不影响扫描）: {e}")

    if scan_only or not standards:
        return standards

    # === 阶段2: 提取/下载 ===
    if task_manager and task_id:
        task_manager.update(task_id,
            progress=progress_base + progress_per_scan + 1,
            dl_progress=0,
            message=f"下载中{type_label}...")

    # 创建 per-item 回调：每处理一条标准后实时推送 stats 和 std_items
    _item_counter = [0]
    _push_interval = 5

    async def _on_item_done(item_name=''):
        _item_counter[0] += 1
        if task_manager and task_id:
            new_stats = compute_download_stats(standards)
            kwargs = {'stats': new_stats, 'persist_std_items': False}
            if _item_counter[0] % _push_interval == 0:
                kwargs['std_items'] = standards
                kwargs['persist_std_items'] = True  # 携带 std_items 时全量持久化
            if item_name:
                kwargs['currentItem'] = item_name
            task_manager.update(task_id, **kwargs)

    if scan_type == 'gb':
        # hcno 提取已合并到 download_phase 串行阶段，不再独立调用 extract_hcno
        # 这样进度条从扫描完成后持续移动，不再有独立的"提取链接"卡顿阶段
        await download_phase(standards, allow_preview_override=config.get('allow_preview'),
                             on_progress=_on_dl_progress, on_item_done=_on_item_done,
                             check_pause=_check_pause)
    elif scan_type == 'hb':
        await download_hb_standards(standards, on_progress=_on_dl_progress,
                                    on_item_done=_on_item_done, check_pause=_check_pause)
    elif scan_type == 'db':
        await download_db_standards(standards, on_progress=_on_dl_progress,
                                    on_item_done=_on_item_done, check_pause=_check_pause)

    if task_manager and task_id:
        task_manager.update(task_id,
            progress=progress_base + progress_per_scan + progress_per_download,
            dl_progress=100,
            message=f"{type_label}下载完成({len(standards)}条)",
            stats=compute_download_stats(standards),
            std_items=standards)

    _log.info(f"[ENGINE] {scan_type.upper()} 下载完成: {len(standards)} 条")
    return standards
