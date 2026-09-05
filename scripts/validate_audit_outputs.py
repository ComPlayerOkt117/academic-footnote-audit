#!/usr/bin/env python3
"""Verify that user-facing audit outputs came from the canonical renderer."""
import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

FOOTNOTE_HEADERS = ['稿件页码','注号','脚注内容','引文（引号内为逐字核对对象）','引注分类','引文关系','母文献题名（含卷册）','析出文献','标称页码','原书页码(已核对)','处理状态','差异 / 疑点','需要你处理']
SOURCE_HEADERS = ['作者/编者','母文献题名（含卷册/期次）','文献类型','出版社/期刊名','年份/日期','期数/版面','对应脚注位置（第X页脚注X）','材料状态','材料提供方式','材料路径']

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('audit_root', type=Path)
    ap.add_argument('skill_root', type=Path)
    args = ap.parse_args()
    root = args.audit_root.resolve()
    skill = args.skill_root.resolve()
    errors = []
    records = root / '04_工作文件' / 'canonical_records.json'
    if not records.exists(): records = root / '03_核对记录' / 'canonical_records.json'
    manifest_path = root / '04_工作文件' / '生成记录.json'
    renderer = skill / 'scripts' / 'render_audit_html.py'
    for p, label in ((records, 'canonical_records.json'), (manifest_path, '生成记录.json'), (renderer, 'canonical renderer')):
        if not p.exists(): errors.append(f'{label} missing')
    if errors:
        for e in errors: print('BLOCKED:', e)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    raw_data = json.loads(records.read_text(encoding='utf-8'))
    if isinstance(raw_data, list):
        data = {'footnotes': raw_data, 'sources': []}
    else:
        data = raw_data if isinstance(raw_data, dict) else {'footnotes': [], 'sources': []}
    footnotes = data.get('footnotes')
    sources = data.get('sources')
    if not isinstance(footnotes, list) or not isinstance(sources, list):
        errors.append('canonical records must contain explicit footnotes and sources lists')
    else:
        if manifest.get('footnote_count') != len(footnotes):
            errors.append('manifest footnote count disagrees with canonical records')
        if manifest.get('source_count') != len(sources):
            errors.append('manifest source count disagrees with canonical records')
    if Path(manifest.get('generator','')).resolve() != renderer: errors.append('manifest generator path is not installed renderer')
    if manifest.get('generator_sha256') != digest(renderer): errors.append('generator hash mismatch')
    if manifest.get('records_sha256') != digest(records): errors.append('records hash mismatch')
    outdir = root / '03_核对记录'
    outputs = ['脚注逐条清单.html','脚注逐条清单.csv','待核文献准备清单.html','待核文献准备清单.csv']
    for name in outputs:
        p = outdir / name
        if not p.exists(): errors.append(f'missing output: {name}')
        elif manifest.get('outputs', {}).get(name) != digest(p): errors.append(f'output hash mismatch: {name}')
    try:
        for name, expected in [('脚注逐条清单.csv', FOOTNOTE_HEADERS), ('待核文献准备清单.csv', SOURCE_HEADERS)]:
            with (outdir / name).open(encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f)
                actual = next(reader)
                rows_in_csv = list(reader)
            if actual != expected: errors.append(f'noncanonical CSV header: {name}')
            expected_rows = len(footnotes) if name.startswith('脚注') else len(sources)
            if len(rows_in_csv) != expected_rows: errors.append(f'CSV row count mismatch: {name}')
        for name, expected in [('脚注逐条清单.html', len(FOOTNOTE_HEADERS)), ('待核文献准备清单.html', len(SOURCE_HEADERS))]:
            h = (outdir / name).read_text(encoding='utf-8')
            if 'generated-by: academic-footnote-audit canonical renderer' not in h: errors.append(f'canonical marker missing: {name}')
            if h.count('<table>') != 1 or len(re.findall(r'<th\b', h)) != expected or len(re.findall(r'<col\b', h)) != expected: errors.append(f'HTML structure mismatch: {name}')
            if any(x in h for x in ('overflow-x:auto','overflow-x:scroll','line-clamp','text-overflow')): errors.append(f'forbidden scroll/truncation: {name}')
            if 'position:sticky' not in h or 'overflow-y:auto' not in h: errors.append(f'sticky table container missing: {name}')
            expected_rows = len(footnotes) if name.startswith('脚注') else len(sources)
            # 表头占 1 个 <tr>；数据行数 = <tr> 总数 - 1。不能用 <tbody><tr> 计数，
            # 因为只有首个数据行紧跟 <tbody>，其余数据行前是 </tr>。
            if h.count('<tr>') - 1 != expected_rows: errors.append(f'HTML row count mismatch: {name}')
    except Exception as exc: errors.append(f'output inspection failed: {exc}')
    if errors:
        for e in errors: print('BLOCKED:', e)
        return 2
    code_files = [p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in
                  {'.py','.pyw','.ps1','.bat','.cmd','.js','.mjs','.cjs','.vbs','.sh','.exe','.dll'}]
    # 一次性工具脚本的存在不构成输出闸门失败。输出可信度由上方哈希/表头/行数校验
    # 保证；脚本只要产出中间记录、未冒充正式成果，就仅作提示记录。
    if code_files:
        for p in code_files:
            print('NOTE: project-local code file present (tool script, recorded only):', p)
    print('PASS: canonical output provenance and structure verified')
    return 0

if __name__ == '__main__': sys.exit(main())
