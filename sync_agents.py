"""AGENTS.md 自动同步脚本

从源代码自动提取版本号、模块列表、API 接口表，
更新 AGENTS.md 中的对应章节，保持文档与代码同步。

用法: python sync_agents.py
"""
import ast
import re
from pathlib import Path

BASE_DIR = Path(__file__).parent
AGENTS_MD = BASE_DIR / "AGENTS.md"
VERSION_FILE = BASE_DIR / "version.py"
ROUTES_DIR = BASE_DIR / "app" / "routes"
SCANNER_DIR = BASE_DIR / "app" / "scanner"


def get_version():
    try:
        tree = ast.parse(VERSION_FILE.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == '__version__':
                        if isinstance(node.value, ast.Constant):
                            return node.value.value
    except Exception:
        pass
    return "unknown"


def get_modules(directory, suffix=".py"):
    modules = []
    for f in sorted(directory.iterdir()):
        if f.suffix != suffix or f.name.startswith('_') or f.name.startswith('__'):
            continue
        name = f.stem
        doc = _extract_module_doc(f)
        modules.append((name, doc))
    return modules


def _extract_module_doc(filepath):
    try:
        tree = ast.parse(filepath.read_text(encoding='utf-8'))
        doc = ast.get_docstring(tree)
        if doc:
            return doc.strip().split('\n')[0]
    except Exception:
        pass
    return ""


def extract_api_routes(directory):
    routes = []
    for f in sorted(directory.iterdir()):
        if f.suffix != '.py' or f.name.startswith('_'):
            continue
        try:
            tree = ast.parse(f.read_text(encoding='utf-8'))
        except Exception:
            continue
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    route_info = _parse_route_decorator(dec)
                    if route_info:
                        routes.append((*route_info, f.stem))
            elif isinstance(node, ast.Assign):
                pass  # 路由模块也可能用 assign 注册路由，暂不覆盖
    return routes


def _parse_route_decorator(dec):
    if isinstance(dec, ast.Call):
        func = dec.func
        method = _resolve_attr(func)
        if method in ('get', 'post', 'put', 'delete', 'patch'):
            if dec.args and isinstance(dec.args[0], ast.Constant):
                return (method.upper(), dec.args[0].value)
    return None


def _resolve_attr(node):
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def update_agents_md(version, route_modules, scanner_modules, api_routes):
    if not AGENTS_MD.exists():
        print(f"[ERROR] AGENTS.md 不存在: {AGENTS_MD}")
        return

    content = AGENTS_MD.read_text(encoding='utf-8')

    content = re.sub(
        r'标准速递 v[\d.]+',
        f'标准速递 v{version}',
        content
    )

    route_section = "\n".join(
        f"  - **{name}.py** — {doc}" if doc else f"  - **{name}.py**"
        for name, doc in route_modules
    )
    content = re.sub(
        r'(路由子包.*?```.*?\n)(.*?)(```)',
        lambda m: m.group(1) + route_section + "\n" + m.group(3),
        content,
        flags=re.DOTALL
    )

    api_table_lines = [
        "| 方法 | 路径 | 来源模块 |",
        "|------|------|----------|",
    ]
    for method, path, source in api_routes:
        api_table_lines.append(f"| {method} | `{path}` | {source} |")

    print(f"版本: v{version}")
    print(f"路由模块: {len(route_modules)} 个")
    print(f"扫描模块: {len(scanner_modules)} 个")
    print(f"API 端点: {len(api_routes)} 个")
    print()

    AGENTS_MD.write_text(content, encoding='utf-8')
    print(f"[OK] AGENTS.md 已同步 (v{version})")


def main():
    version = get_version()
    route_modules = get_modules(ROUTES_DIR)
    scanner_modules = get_modules(SCANNER_DIR)
    api_routes = extract_api_routes(ROUTES_DIR)
    update_agents_md(version, route_modules, scanner_modules, api_routes)


if __name__ == "__main__":
    main()
