"""关键词匹配模块测试 — v3.5.0 扩展

增加 load_keywords(filepath) 文件读取测试
"""
import sys
import os
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.keywords import SafetyMatcher, is_safety, is_aq_yj, clean_name, load_keywords


class TestSafetyMatcher:
    def test_basic_match(self):
        matcher = SafetyMatcher(['安全', '消防'], [])
        assert matcher.is_safety('安全生产规范')
        assert matcher.is_safety('消防安全管理')

    def test_exclude_first(self):
        matcher = SafetyMatcher(['安全', '消防'], ['信息安全'])
        assert not matcher.is_safety('信息安全管理办法')
        assert matcher.is_safety('安全生产规范')

    def test_no_match(self):
        matcher = SafetyMatcher(['爆炸', '有毒'], [])
        assert not matcher.is_safety('普通办公管理规定')

    def test_multiple_excludes(self):
        matcher = SafetyMatcher(['安全'], ['信息安全', '食品安全'])
        assert not matcher.is_safety('食品安全条例')
        assert not matcher.is_safety('信息安全规范')
        assert matcher.is_safety('安全管理条例')

    def test_longer_keyword_first(self):
        matcher = SafetyMatcher(['安全', '压力容器'], [])
        assert matcher.is_safety('压力容器检验规范')

    def test_case_insensitive(self):
        matcher = SafetyMatcher(['安全'], [])
        assert matcher.is_safety('安全')

    def test_empty_input(self):
        matcher = SafetyMatcher(['安全'], [])
        assert not matcher.is_safety('')
        assert not matcher.is_safety(None)


class TestIsAqYj:
    def test_aq_code(self):
        assert is_aq_yj('AQ 8001')
        assert is_aq_yj('AQ/T 9001')

    def test_yj_code(self):
        assert is_aq_yj('YJ/T 001')

    def test_other_code(self):
        assert not is_aq_yj('GB/T 12345')
        assert not is_aq_yj('')


class TestCleanName:
    def test_remove_sacinfo(self):
        assert clean_name('<sacinfo>abc</sacinfo>') == 'abc'

    def test_no_sacinfo(self):
        assert clean_name('hello world') == 'hello world'

    def test_empty(self):
        assert clean_name('') == ''
        assert clean_name(None) == ''


class TestLoadKeywords:
    def test_load_from_file(self):
        """验证从文件读取关键词"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
            f.write("安全\n")
            f.write("消防\n")
            f.write("# 这是注释\n")
            f.write("\n")
            f.write("压力容器\n")
            filepath = f.name
        try:
            keywords = load_keywords(filepath)
            assert '安全' in keywords
            assert '消防' in keywords
            assert '压力容器' in keywords
            assert len(keywords) == 3
        finally:
            os.unlink(filepath)

    def test_load_from_nonexistent_file(self):
        """验证不存在的文件回退到默认"""
        keywords = load_keywords('/nonexistent/path/keywords.txt')
        assert isinstance(keywords, list)
        assert len(keywords) > 0

    def test_load_default(self):
        """验证无参数时从配置加载"""
        keywords = load_keywords()
        assert isinstance(keywords, list)
