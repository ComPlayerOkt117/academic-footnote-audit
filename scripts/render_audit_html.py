#!/usr/bin/env python3
"""Generate the two audit HTML pages from one canonical JSON record set."""
import argparse
import csv
import hashlib
import html
import json
import runpy
import sys
from datetime import datetime, timezone
from pathlib import Path

CSS = ("body{font-family:'Microsoft YaHei',Arial,sans-serif;background:#fff;color:#222;margin:24px;line-height:1.5}"
       "h1{font-size:22px;color:#17324d;margin:0 0 8px}p.status{font-size:13px;color:#555;margin:0 0 10px}"
       ".switch{margin:0 0 16px;padding:8px 12px;background:#f3f7fa;border-left:4px solid #477b9e}"
       ".switch a{color:#1a5a8a;text-decoration:none;margin-right:20px}"
       ".legend{font-size:12px;color:#444;margin:0 0 12px;background:#fafbfc;padding:6px 10px;border:1px dashed #c8d0d8}"
       ".table-wrap{width:100%;max-height:calc(100vh - 170px);overflow-y:auto;overflow-x:hidden;position:relative}"
       "table{border-collapse:separate;border-spacing:0;width:100%;table-layout:fixed;background:#fff}"
       "th,td{box-sizing:border-box;border:1px solid #cfd6dc;padding:7px 8px;vertical-align:top;word-break:break-word;white-space:normal;font-size:12px;line-height:1.5}"
       "th{background:#eaf1f7;color:#17324d;position:sticky;top:0;z-index:2;text-align:left}"
       "td{overflow-wrap:anywhere}tbody tr:nth-child(even){background:#fafcfd}strong{font-weight:700}a{color:#1a5a8a}"
       ".st{display:inline-block;border-radius:4px;padding:1px 8px;font-weight:700;font-size:11px;white-space:nowrap;color:#fff}"
       ".st-ok{background:#1a7f37}.st-warn{background:#d4a72c}.st-wait{background:#6e7681}.st-info{background:#0969da}.st-red{background:#cf222e}"
       "td.dash{color:#9aa4ad}.td-issue{background:#fff7e6}")

# 面向作者的清单列：只显示对核对/找文献有直接意义的项目。
# 内部字段（source_key/edition_key/source_page_pdf/evidence_locator/marker_locator…）保留在 JSON 数据中，不显示。
FOOTNOTE = [
    ('稿件页码', 'page'), ('注号', 'marker'), ('脚注内容', 'footnote_content'),
    ('引文（引号内为逐字核对对象）', 'full_preceding_citation_span'),
    ('引注分类', 'citation_type'), ('引文关系', 'citation_scope_relation'),
    ('母文献题名（含卷册）', 'parent_work_title_with_volume_or_part'),
    ('析出文献', 'excerpted_work_titles'),
    ('标称页码', 'source_page_cited'), ('原书页码(已核对)', 'source_page_printed'),
    ('处理状态', 'handling_status'), ('差异 / 疑点', 'issue'), ('需要你处理', 'next_action')]

# 待核文献清单：面向"去找书核对"的作者。析出文献与该目的无关（一本书对应多个析出、列不现实）；
# 材料提供方式/路径已隐含材料是否到位，故无需"下一步"列。
SOURCE = [
    ('作者/编者', 'author_editor'),
    ('母文献题名（含卷册/期次）', 'parent_work_title_with_volume_or_part'),
    ('文献类型', 'source_type'), ('出版社/期刊名', 'publisher_or_journal'),
    ('年份/日期', 'publication_year_or_date'), ('期数/版面', 'issue_number'),
    ('对应脚注位置（第X页脚注X）', 'footnote_locations'), ('材料状态', 'material_status'),
    ('材料提供方式', 'material_route'), ('材料路径', 'material_path')]

# Explicit widths keep both manuscript types visually identical. The table must
# fit the viewport; long content wraps inside cells and is never horizontally
# scrolled or clamped.
FOOTNOTE_WIDTHS = [3, 3, 9, 14, 4, 4, 10, 6, 3, 3, 7, 13, 13]
SOURCE_WIDTHS = [11, 22, 5, 12, 7, 6, 14, 6, 7, 10]

MAP = {'direct': '直接引文', 'indirect': '间接引文', 'uncertain': '待确认', 'book': '图书',
       'journal': '期刊', 'newspaper': '报纸', 'archive': '档案', 'official document': '正式文件',
       'web source': '网络来源', 'unknown': '未确定', 'already supplied': '已提供',
       'partially supplied': '部分提供', 'missing': '缺失', 'ambiguous identity': '文献身份待确认',
       'not needed for current check': '当前核对不需要', 'needs-source': '缺失',
       'external path': '外部路径', 'local folder': '本地文件夹'}

# 处理状态 → (前缀符号, CSS class)。让作者一眼分辨：绿=已核实，黄=需你定夺，灰=待补材料。
STATUS_STYLE = {
    '已核实': ('✅', 'st-ok'),
    '用户已核对': ('✅', 'st-ok'),
    '待人工复核': ('⚠️', 'st-warn'),
    '待人工补判': ('❓', 'st-warn'),
    '待补原文': ('⏳', 'st-wait'),
    '待来源': ('⏳', 'st-wait'),
    '待盘点': ('⏳', 'st-wait'),
    '待视觉复核': ('👁️', 'st-wait'),
    '用户自行核对': ('👤', 'st-info'),
    '不适用': ('—', 'st-wait'),
}
STATUS_LEGEND = '状态图例：✅ 已核实 ｜ ⚠️ 待人工复核(有疑点需你定夺) ｜ ⏳ 待补原文(材料未提供) ｜ 👁️ 待视觉复核 ｜ — 不适用'


def status_style(value):
    """处理状态 → (emoji, css-class)；未登记状态原样返回无徽章。"""
    v = str(value or '').strip()
    if v in STATUS_STYLE:
        emoji, cls = STATUS_STYLE[v]
        return emoji, cls
    return '', ''

# 旧版本数据的兼容字段名（现行规范一律使用英文标准 key；此处只认历史别名）
LEGACY_KEY = {'footnote_content': 'footnote_text',
              'full_preceding_citation_span': 'preceding_text_span',
              'source_file_status': 'status'}

def cell_value(row, key, escape_html=False):
    """取某列在行内的值（标准 key 优先，历史别名兜底），列表拼接为'；'分隔，
    再经 MAP 把英文值映射为中文展示；escape_html=True 时做 HTML 转义（CSV 不转义）。"""
    value = row.get(key) if row.get(key) is not None else row.get(LEGACY_KEY.get(key, ''))
    if value is None:
        value = ''
    if isinstance(value, list):
        value = '；'.join(str(item) for item in value)
    value = MAP.get(str(value), str(value))
    return html.escape(value) if escape_html else value

def render_csv(rows, columns, output):
    validate_columns(columns, rows)
    with output.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow([label for label, _ in columns])
        for row in rows:
            values = []
            for _, key in columns:
                v = str(cell_value(row, key) or '').strip()
                if key == 'handling_status':
                    emoji, _cls = status_style(v)
                    v = f'{emoji} {v}' if emoji else v
                values.append(v)
            writer.writerow(values)

def validate_columns(columns, rows):
    """防御：行内只允许技能定义的字段（含文档承诺的内部字段与 legacy 别名），
    未知字段名会静默丢列，必须显式报错而非悄悄忽略。"""
    keys = [key for _, key in columns]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate visible column key')
    allowed = {
        # 展示与文档字段
        'page','marker','footnote_content','full_preceding_citation_span','citation_type',
        'citation_scope_relation','parent_work_title_with_volume_or_part','excerpted_work_titles',
        'source_page_cited','source_page_printed','handling_status','issue','next_action',
        # 内部字段（outputs.md 承诺保留，不展示）
        'source_key','source_page_pdf','edition_key','evidence_locator','marker_locator',
        'paragraph_id','marker_order','boundary_confidence','text_source','ocr_conflict',
        'source_as_cited','span_boundary_reason','author_editor','source_type',
        'publisher_or_journal','publication_year_or_date','issue_number','source_file_status',
        'material_status','material_route','material_path','footnote_locations','status',
        # legacy 别名（兼容旧版英文 key）
        'footnote_text','preceding_text_span',
    }
    for row in rows:
        unexpected = set(row) - allowed
        if unexpected:
            raise ValueError(f'unexpected canonical fields in row: {sorted(unexpected)}')

def render(title, status, rows, columns, output, widths, bold_key=None):
    validate_columns(columns, rows)
    if len(widths) != len(columns):
        raise ValueError('width count does not match visible column count')
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
             f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body>',
             '<!-- generated-by: academic-footnote-audit canonical renderer -->',
             f'<h1>{html.escape(title)}</h1><p class="status">{html.escape(status)}</p>',
             '<div class="switch"><a href="脚注逐条清单.html">脚注逐条清单</a>'
             '<a href="待核文献准备清单.html">待核文献准备清单</a></div>']
    if any(k == 'handling_status' for _, k in columns):
        parts.append(f'<p class="legend">{STATUS_LEGEND}</p>')
    parts.append('<div class="table-wrap"><table><colgroup>')
    total_width = sum(widths)
    parts.extend(f'<col style="width:{width / total_width * 100:.4f}%">' for width in widths)
    parts.append('</colgroup><thead><tr>')
    parts.extend(f'<th>{html.escape(label)}</th>' for label, _ in columns)
    parts.append('</tr></thead><tbody>')
    for row in rows:
        parts.append('<tr>')
        for _, key in columns:
            if key == 'handling_status':
                v = str(cell_value(row, key) or '').strip()
                emoji, cls = status_style(v)
                cell = (f'<span class="st {cls}">{emoji} {html.escape(v)}</span>' if cls
                        else html.escape(v))
            else:
                v = str(cell_value(row, key) or '').strip()
                if key in ('issue', 'next_action') and not v:
                    cell = '<span class="dash">—</span>'
                else:
                    cell = cell_value(row, key, escape_html=True)
            if key == bold_key:
                cell = f'<strong>{cell}</strong>'
            td_cls = ' class="td-issue"' if key == 'issue' and str(cell_value(row, key) or '').strip() else ''
            parts.append(f'<td{td_cls}>{cell}</td>')
        parts.append('</tr>')
    parts.append('</tbody></table></div></body></html>')
    output.write_text(''.join(parts), encoding='utf-8', newline='\n')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('records', type=Path)
    parser.add_argument('output_dir', type=Path)
    args = parser.parse_args()
    # The records file is the authoritative project anchor.  Do not trust a
    # separately passed output path: on Windows, non-ASCII subprocess
    # arguments can be corrupted and make the renderer write beside the real
    # audit workspace.  The fixed output directory is derived here instead.
    args.records = args.records.resolve()
    args.output_dir = args.records.parent.parent / '03_核对记录'
    raw_data = json.loads(args.records.read_text(encoding='utf-8'))
    # Schema tolerance: accept a bare list of footnotes (wrapped here) so the
    # renderer fails with a clear structure error instead of an AttributeError.
    if isinstance(raw_data, list):
        data = {'footnotes': raw_data, 'sources': [], 'status': '初步盘点结果'}
    else:
        data = raw_data if isinstance(raw_data, dict) else {'footnotes': [], 'sources': [], 'status': '初步盘点结果'}
    validator = Path(__file__).with_name('validate_audit_records.py')
    old_argv = sys.argv
    try:
        sys.argv = [str(validator), str(args.records)]
        runpy.run_path(str(validator), run_name='__main__')
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code:
            raise RuntimeError(f'fixed record validation failed ({code})') from exc
    finally:
        sys.argv = old_argv
    args.output_dir.mkdir(parents=True, exist_ok=True)
    status = str(data.get('status', '初步盘点结果'))
    footnotes = data.get('footnotes', [])
    sources = data.get('sources', [])
    if not isinstance(footnotes, list) or not isinstance(sources, list):
        raise ValueError('canonical records must contain explicit list-valued footnotes and sources; renderer will not reconstruct source identity')
    render('脚注逐条清单', status, footnotes, FOOTNOTE,
           args.output_dir / '脚注逐条清单.html', FOOTNOTE_WIDTHS)
    render_csv(footnotes, FOOTNOTE, args.output_dir / '脚注逐条清单.csv')
    render('待核文献准备清单', status, sources, SOURCE,
           args.output_dir / '待核文献准备清单.html', SOURCE_WIDTHS,
           'parent_work_title_with_volume_or_part')
    render_csv(sources, SOURCE, args.output_dir / '待核文献准备清单.csv')

    def sha256(path):
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    # 03_核对记录 is directly below the project root.
    audit_root = args.output_dir.parent
    manifest_dir = audit_root / '04_工作文件'
    manifest_dir.mkdir(parents=True, exist_ok=True)
    outputs = [args.output_dir / name for name in (
        '脚注逐条清单.html', '脚注逐条清单.csv',
        '待核文献准备清单.html', '待核文献准备清单.csv')]
    manifest = {
        'generator': str(Path(__file__).resolve()),
        'generator_sha256': sha256(Path(__file__).resolve()),
        'records': str(args.records.resolve()),
        'records_sha256': sha256(args.records.resolve()),
        'schema': 'academic-footnote-audit/2026-09',
        'footnote_count': len(footnotes),
        'source_count': len(sources),
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'outputs': {str(path.name): sha256(path) for path in outputs},
    }
    (manifest_dir / '生成记录.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')

if __name__ == '__main__':
    main()
