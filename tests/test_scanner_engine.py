"""扫描引擎模块测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scanner_engine import run_scan_pipeline
from app.routes._utils import create_combined_scan_tasks


class TestRunScanPipeline:
    def test_function_exists(self):
        assert callable(run_scan_pipeline)

    def test_create_combined_scan_tasks_exists(self):
        assert callable(create_combined_scan_tasks)

    def test_gb_config_defaults(self):
        """验证 GB 配置默认值"""
        config = {'max_results': 100, 'scan_only': True}
        # 只验证函数签名和导入不报错
        import asyncio
        import inspect
        sig = inspect.signature(run_scan_pipeline)
        params = list(sig.parameters.keys())
        assert 'scan_type' in params
        assert 'config' in params
        assert 'task_id' in params
        assert 'task_manager' in params
