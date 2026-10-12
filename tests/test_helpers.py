"""工具函数测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.helpers import safe_filename, format_bytes, format_duration, validate_path, normalize_code


class TestSafeFilename:
    def test_normal_name(self):
        assert safe_filename('GB 12345-2024') == 'GB 12345-2024'

    def test_strip_invalid_chars(self):
        result = safe_filename('test<file>:name.pdf')
        assert '<' not in result
        assert '>' not in result
        assert ':' not in result
        assert result.endswith('.pdf')

    def test_empty(self):
        assert safe_filename('') == 'unnamed'

    def test_length_limit(self):
        long_name = 'x' * 200 + '.pdf'
        result = safe_filename(long_name)
        assert len(result) <= 160

    def test_path_traversal_blocked(self):
        result = safe_filename('../etc/passwd')
        assert '..' not in result

    def test_halfwidth_punct_to_fullwidth(self):
        assert safe_filename('第5部分: 智能协同巡检') == '第5部分： 智能协同巡检'
        assert safe_filename('规则(试行).pdf') == '规则（试行）.pdf'

    def test_newline_to_space(self):
        assert safe_filename('AB\nCD') == 'AB CD'

    def test_collapse_repeated_chars(self):
        assert safe_filename('电梯检验规则——液压电梯') == '电梯检验规则-液压电梯'
        assert safe_filename('A    B') == 'A B'

    def test_confused_part_number(self):
        assert safe_filename('GA/T 2075.l-2023 第l部分:溶剂提取') == 'GAT 2075.1-2023 第1部分：溶剂提取'

    def test_normalize_code_part_number(self):
        assert normalize_code('GA/T 2075.l—2023') == 'GA/T 2075.1-2023'


class TestFormatBytes:
    def test_bytes(self):
        assert format_bytes(500) == '500 B'

    def test_kb(self):
        assert 'KB' in format_bytes(2048)

    def test_mb(self):
        assert 'MB' in format_bytes(5 * 1024 * 1024)


class TestFormatDuration:
    def test_seconds(self):
        assert '秒' in format_duration(30)

    def test_minutes(self):
        result = format_duration(90)
        assert '分' in result

    def test_hours(self):
        result = format_duration(7200)
        assert '小时' in result


class TestValidatePath:
    def test_valid_path(self):
        result = validate_path(str(Path.home()))
        assert result is not None

    def test_empty(self):
        assert validate_path('') is None

    def test_none(self):
        assert validate_path(None) is None
