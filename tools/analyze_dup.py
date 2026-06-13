"""分析标准规范目录中的重复/近重复 PDF 文件 — 双击运行默认路径，支持 CLI 指定目录或导出"""
import sys
import csv
import json
import hashlib
import re
from datetime import datetime
from pathlib import Path
from collections import defaultdict

# 默认分析目录（双击运行时使用，CLI 可通过参数覆盖）
DEFAULT_TARGET = Path(r"E:\标准规范\标准规范-2025\法律法规、标准规范")


def hash_file(path, chunk_size=65536):
    """快速 hash，只读前 64KB + 后 64KB + 文件大小"""
    size = path.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())
    try:
        with open(path, 'rb') as f:
            if size <= 128 * 1024:
                h.update(f.read())
            else:
                h.update(f.read(65536))
                f.seek(-65536, 2)
                h.update(f.read(65536))
    except Exception:
        return None
    return h.hexdigest()


def _progress(current, total, label=""):
    pct = current * 100 // total
    print(f"\r  {label}{current}/{total} ({pct}%)", end="", flush=True)


def analyze(target: Path, output_format: str = None):
    if not target.is_dir():
        print(f"目录不存在: {target}")
        return

    pdfs = sorted(target.glob("*.pdf"))
    print(f"目录: {target}")
    print(f"总文件数: {len(pdfs)}")
    if not pdfs:
        print("无 PDF 文件。")
        return

    # ── 1. 大小分组 ──
    print(f"\n{'='*60}")
    print("第一步：按文件大小分组...")
    size_map = defaultdict(list)
    for i, p in enumerate(pdfs):
        if (i + 1) % 200 == 0:
            _progress(i + 1, len(pdfs), "大小扫描 ")
        size_map[p.stat().st_size].append(p)
    print()

    # ── 2. hash 确认 ──
    print("第二步：对同大小文件进行 hash 校验...")
    exact_dup_groups = []
    candidates = [(sz, fs) for sz, fs in size_map.items() if len(fs) > 1]
    done = 0
    total_hashes = sum(len(fs) for _, fs in candidates)
    for size, files in candidates:
        hash_groups = defaultdict(list)
        for f in files:
            h = hash_file(f)
            if h:
                hash_groups[h].append(f)
            done += 1
            if done % 50 == 0 and total_hashes > 0:
                _progress(done, total_hashes, "hash校验 ")
        for h, g in hash_groups.items():
            if len(g) > 1:
                exact_dup_groups.append(g)
    if total_hashes > 0:
        print()

    print(f"\n{'='*60}")
    print("【一、精确重复】完全相同的文件（相同大小+相同内容）")
    print(f"共 {len(exact_dup_groups)} 组，涉及 {sum(len(g) for g in exact_dup_groups)} 个文件")
    for i, group in enumerate(exact_dup_groups, 1):
        size_mb = group[0].stat().st_size / 1024 / 1024
        print(f"\n  组{i}: ({size_mb:.2f} MB)")
        for f in group:
            mtime = f.stat().st_mtime
            dt = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            print(f"    {f.name}  [{dt}]")

    # ── 3. 同编号不同文件名 ──
    code_pat = re.compile(r'^([A-Z]+\s*\d[\d.]*)\s*[-–—－−‐‑‒]\s*(\d{4})')
    code_groups = defaultdict(list)
    for p in pdfs:
        m = code_pat.match(p.name)
        if m:
            code = f"{m.group(1).strip()} {m.group(2)}"
            code_groups[code].append(p)

    near_dup = {k: v for k, v in code_groups.items() if len(v) > 1}
    print(f"\n{'='*60}")
    print("【二、同编号不同文件名】同一标准编号但文件名不同的文件")
    print(f"共 {len(near_dup)} 组，涉及 {sum(len(v) for v in near_dup.values())} 个文件")
    for code, files in sorted(near_dup.items()):
        print(f"\n  编号: {code}")
        for f in files:
            print(f"    {f.name}")

    # ── 3.5 同标准不同年份版本 ──
    base_pat = re.compile(r'^([A-Z]+\s*\d[\d.]*)\s*[-–—－−‐‑‒]\s*(\d{4})')
    base_groups = defaultdict(set)
    for p in pdfs:
        m = base_pat.match(p.name)
        if m:
            prefix_key = m.group(1).strip()
            base_groups[prefix_key].add(int(m.group(2)))

    multi_year = {k: sorted(v) for k, v in base_groups.items() if len(v) > 1}
    if multi_year:
        print(f"\n{'='*60}")
        print("【二点五、同标准多版本】同一标准前缀存在多个年份版本")
        print(f"共 {len(multi_year)} 个标准存在多版本")
        for prefix, years in sorted(multi_year.items()):
            years_str = ', '.join(str(y) for y in years)
            print(f"  {prefix}: {years_str}")

    # ── 4. 文件名异常 ──
    print(f"\n{'='*60}")
    print("【三、文件名异常】")
    anomalies = []
    SPECIAL_DASHES = {'—', '–', '―', '－', '−'}

    for p in pdfs:
        name = p.name
        issues = []
        if name.endswith('.pdf.pdf'):
            issues.append("双扩展名 .pdf.pdf")
        if name.endswith(' .pdf'):
            issues.append("末尾空格")
        if '..' in name:
            issues.append("双句号")
        stem = p.stem
        if '  ' in stem:
            issues.append(f"多余空格: {repr(stem)}")
        if any(ch in stem for ch in SPECIAL_DASHES):
            found = [ch for ch in SPECIAL_DASHES if ch in stem]
            issues.append(f"含特殊连字符: {repr(found[0])}")
        if re.match(r'^GBT[\s\d]', name):
            issues.append("GBT 格式（可能应为 GB/T）")

        if issues:
            anomalies.append((p, issues))

    for f, issues in sorted(anomalies, key=lambda x: (x[1][0], x[0].name)):
        print(f"  {f.name}  ← {', '.join(issues)}")

    print(f"共 {len(anomalies)} 个异常文件名")

    # ── 5. 按标准类型统计 ──
    print(f"\n{'='*60}")
    print("【四、按标准类型统计】")
    type_counts = defaultdict(int)
    for p in pdfs:
        prefix = p.name.split()[0] if ' ' in p.name else p.name[:5]
        type_counts[prefix] += 1

    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t}: {c} 个")

    total_gb = sum(p.stat().st_size for p in pdfs) / 1024 / 1024 / 1024
    print(f"\n总计: {len(pdfs)} 个 PDF 文件")
    print(f"目录总大小: {total_gb:.2f} GB")

    # ── 导出 ──
    if output_format:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if output_format == 'csv':
            _export_csv(target, timestamp, exact_dup_groups, near_dup, anomalies)
        elif output_format == 'json':
            _export_json(target, timestamp, exact_dup_groups, near_dup, anomalies)
    elif len(sys.argv) <= 1:
        print()
        input("按回车键退出...")


def _export_csv(target, timestamp, exact_dup_groups, near_dup, anomalies):
    path = target / f"dup_analysis_{timestamp}.csv"
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(["类型", "文件名", "组", "说明"])
        for i, group in enumerate(exact_dup_groups, 1):
            size_mb = group[0].stat().st_size / 1024 / 1024
            for pf in group:
                w.writerow(["精确重复", pf.name, f"组{i}", f"{size_mb:.2f} MB"])
        for code, files in sorted(near_dup.items()):
            for pf in files:
                w.writerow(["同编号不同名", pf.name, code, ""])
        for pf, issues in sorted(anomalies, key=lambda x: (x[1][0], x[0].name)):
            w.writerow(["文件名异常", pf.name, "", ", ".join(issues)])
    print(f"\nCSV 已导出: {path}")


def _export_json(target, timestamp, exact_dup_groups, near_dup, anomalies):
    path = target / f"dup_analysis_{timestamp}.json"
    data = {
        "target": str(target),
        "analyzed_at": datetime.now().isoformat(),
        "exact_duplicates": [
            {"group": i, "size_mb": round(g[0].stat().st_size / 1024 / 1024, 2),
             "files": [{"name": pf.name, "mtime": datetime.fromtimestamp(pf.stat().st_mtime).isoformat()} for pf in g]}
            for i, g in enumerate(exact_dup_groups, 1)
        ],
        "near_duplicates": [
            {"code": code, "files": [pf.name for pf in files]}
            for code, files in sorted(near_dup.items())
        ],
        "anomalies": [
            {"name": pf.name, "issues": issues}
            for pf, issues in sorted(anomalies, key=lambda x: (x[1][0], x[0].name))
        ],
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\nJSON 已导出: {path}")


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description="分析标准规范目录中的重复/近重复 PDF 文件")
    p.add_argument('target', nargs='?', type=Path, default=DEFAULT_TARGET,
                   help=f'目标目录（默认: {DEFAULT_TARGET}）')
    p.add_argument('--output', '-o', choices=['csv', 'json'],
                   help='导出格式 (csv 或 json)，不指定则仅终端输出')
    args = p.parse_args()
    analyze(args.target, args.output)
