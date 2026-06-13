"""工具函数测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.helpers import safe_filename, format_bytes, format_duration, validate_path


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
