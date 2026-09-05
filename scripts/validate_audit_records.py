#!/usr/bin/env python3
"""Block delivery when canonical footnote records contain unsafe spans."""
import argparse
import json
import re
import sys
from pathlib import Path

TITLE_RE = re.compile(r'^(?:[一二三四五六七八九十百]+、|（[一二三四五六七八九十百]+）|\([一二三四五六七八九十百]+\))')
MARKER_RE = re.compile(r'[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|\[\d{1,3}\]')
QUOTE_PAIRS = [('“','”'), ('「','」'), ('『','』'), ('"','"'), ('“','”'), ('‘','’')]

def quote_ok(text):
    for left, right in QUOTE_PAIRS:
        if left != right and text.count(left) != text.count(right):
            return False
    return True

def quote_shape_ok(text):
    pairs = [('“', '”'), ('「', '」'), ('『', '』'), ('‘', '’')]
    for left, right in pairs:
        if right in text and left not in text:
            return False
        if left in text and right not in text:
            return False
    return True

def source_title_suspicion(row):
    title = str(row.get('parent_work_title_with_volume_or_part') or '').strip()
    if not title or title in ('待归一化', '待核实', '待人工补判', '未提供（脚注所列来源）'):
        return None
    footnote = str(row.get('footnote_content') or row.get('footnote_text') or '').strip()
    compact_title = re.sub(r'\s+', '', title)
    compact_note = re.sub(r'\s+', '', footnote)
    if compact_note and compact_title == compact_note:
        return '母文献题名等于完整脚注文本'
    if any(token in title for token in ('本文系', '阶段性成果', '项目“', '项目"')):
        return '母文献题名混入论文说明或项目说明'
    if re.search(r'\[[MJNADCS]\]', title) or '//' in title:
        return '母文献题名仍包含完整参考文献标记'
    return None

def longest_shared_run(a, b):
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            n = 0
            while i + n < len(a) and j + n < len(b) and a[i+n] == b[j+n]:
                n += 1
            best = max(best, n)
    return best

def manual_review(row):
    value = ' '.join(str(row.get(k, '') or '') for k in
                     ('handling_status', 'status', 'issue', 'next_action'))
    return any(token in value for token in ('人工复核', '待人工', '待确认', '待视觉核验', 'manual', 'ambiguous'))

def span_for(row):
    return str(row.get('full_preceding_citation_span') or
               row.get('preceding_text_span') or '').strip()

# 占位/模板内容探测：拦截"未真实提取、用占位符跑通流水线"的空心盘点。
# 命中任意关键词即视为整份盘点未完成（结构性失败，blocked），提示走视觉/文本兜底重做。
BOILER_MARKERS = ('页面脚注', '映射异常', '页面证据可见', '待按页面逐条复核',
                  '待按页面', '（内容待提取）', '（见页面证据）')

def boilerplate_hit(text):
    t = str(text or '').replace('\u3000', ' ')
    return any(marker in t for marker in BOILER_MARKERS)

def is_boilerplate_row(row):
    note = str(row.get('footnote_content') or row.get('footnote_text') or '').strip()
    span = span_for(row)
    return boilerplate_hit(note) or boilerplate_hit(span)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('records', type=Path)
    args = ap.parse_args()
    raw = json.loads(args.records.read_text(encoding='utf-8'))
    # Schema tolerance: model may produce a bare list of footnotes instead of
    # {footnotes: [...]}. Wrap it instead of crashing with an AttributeError.
    if isinstance(raw, list):
        data = {'footnotes': raw, 'sources': []}
    else:
        data = raw if isinstance(raw, dict) else {'footnotes': [], 'sources': []}
    rows = data.get('footnotes', [])
    findings = []
    fatal = []
    if not isinstance(rows, list) or not rows:
        fatal.append('canonical footnotes list is missing or empty')
    if not isinstance(data.get('sources'), list):
        fatal.append('canonical sources list is missing; source list may not be reconstructed during rendering')
    groups = {}
    placeholder_rows = []
    for i, row in enumerate(rows, 1):
        if is_boilerplate_row(row):
            placeholder_rows.append(i)
        span = span_for(row)
        issue = str(row.get('issue', '') or '')
        status = str(row.get('handling_status', '') or '')
        review = manual_review(row)
        if not span:
            findings.append((i, 'blocker-row', 'empty citation span'))
        if MARKER_RE.search(span):
            findings.append((i, 'blocker-row', 'citation span contains a possible footnote marker'))
        if TITLE_RE.match(span.lstrip()):
            findings.append((i, 'review', 'citation span starts with a heading pattern'))
        quote_present = any(mark in span for mark in '“”‘’「」『』"\'')
        if quote_present and row.get('citation_type') in ('indirect', '间接引文'):
            findings.append((i, 'review', 'citation span contains quotation marks but is classified as indirect — likely miscategorization: quoted wording inside a span makes it a direct quote; verify type (boundary.md)'))
        if row.get('citation_type') in ('direct', '直接引文') or quote_present:
            inner = span.strip('“”「」『』\"\' ，、。！？；：')
            if span in ('“','”','"',"'") or len(inner) <= 2:
                findings.append((i, 'blocker-row', 'direct quotation is an isolated mark or short fragment'))
            if not quote_ok(span):
                findings.append((i, 'blocker-row', 'direct quotation marks are unbalanced'))
            if not quote_shape_ok(span):
                findings.append((i, 'blocker-row', 'quotation span has an impossible opening/closing quote shape'))
        if span.lstrip().startswith(tuple('”’」』\"\'，、。！？；：')):
            findings.append((i, 'blocker-row', 'citation starts with a closing mark or punctuation fragment'))
        if span.rstrip().endswith(tuple('，、：')):
            findings.append((i, 'review', 'citation ends inside a clause'))
        # span 膨胀提示：引文普遍是短引语单元；过长或含多个完整句，疑把作者论述整段吞入。
        # （bulk 提示不阻断，只进复核队列——与"单条疑问继续、逐条语义复核"一致）
        if len(span) > 80:
            findings.append((i, 'review', 'citation span unusually long (>80 chars); may have swallowed surrounding author text — verify it is a genuine quote unit (semantic, per-boundary judgment, boundary.md)'))
        elif len(re.findall(r'[。！？]', span)) >= 2:
            findings.append((i, 'review', 'citation span contains multiple full sentences; verify it is not padding from preceding prose'))
        conflict = row.get('ocr_conflict')
        if conflict and not any(token in (issue + status) for token in ('人工复核','待人工','manual')):
            findings.append((i, 'review', 'OCR conflict lacks a manual-review status'))
        title_problem = source_title_suspicion(row)
        if title_problem:
            findings.append((i, 'blocker-row', title_problem))
        key = (row.get('paragraph_id'), row.get('marker_order'))
        groups.setdefault(row.get('paragraph_id'), []).append((row.get('marker_order', 0), span, i))
    for paragraph, items in groups.items():
        items.sort()
        for pos in range(1, len(items)):
            prev, cur = items[pos-1], items[pos]
            if len(cur[1]) > len(prev[1]) and prev[1] and prev[1] in cur[1]:
                relation = str(rows[cur[2]-1].get('citation_scope_relation', ''))
                if relation not in ('连续共引','重叠共引'):
                    findings.append((cur[2], 'review', 'later citation is an unexplained strict superset of the prior span'))
            if len(cur[1]) > len(prev[1]) and prev[1] and longest_shared_run(prev[1], cur[1]) >= 16:
                relation = str(rows[cur[2]-1].get('citation_scope_relation', ''))
                if relation not in ('连续共引','重叠共引'):
                    findings.append((cur[2], 'review', 'later citation shares a long run with the prior span and may be cumulative backtracking'))
    if placeholder_rows:
        shown = '、'.join(str(n) for n in placeholder_rows[:5])
        more = f' 等' if len(placeholder_rows) > 5 else ''
        fatal.append(f'{len(placeholder_rows)} 行脚注正文或引文 span 是占位模板（第 {shown}{more} 行，含"页面脚注/映射异常/待按页面逐条复核"等字样）——疑似未真实提取的空心盘点，整份不得交付；请按 evidence.md 重做提取（文本层异常→pdftoppm 渲染走视觉，GBK 乱码→verify_source_quotes --mojibake-gbk），禁止用占位符代替盘点')
    report = {'status':'blocked' if fatal else ('review' if findings else 'passed'),
              'fatal': fatal,
              'finding_count': len(findings),
              'blocker_row_count': sum(1 for _, level, _ in findings if level == 'blocker-row'),
              'review_row_count': sum(1 for _, level, _ in findings if level == 'review'),
              'findings':[{'row':row,'level':level,'message':msg} for row,level,msg in findings]}
    report_path = args.records.parent / '边界检查报告.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
    if fatal:
        for msg in fatal: print(f'BLOCKED: {msg}')
        return 2
    for row, level, msg in findings:
        print(f'WARNING ROW {row} [{level}]: {msg}')
    if findings:
        print(f'PASS WITH REVIEW: {len(rows)} records retained; {len(findings)} row-level findings written to {report_path}')
        return 0
    print(f'PASS: {len(rows)} footnote records passed boundary and OCR gates')
    return 0

if __name__ == '__main__':
    sys.exit(main())
