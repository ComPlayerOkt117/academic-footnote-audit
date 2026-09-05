#!/usr/bin/env python3
"""Normalize canonical footnote rows into explicit parent-source records.

Role boundary (2026-09): source identity is a SEMANTIC task. The model fills
parent_work_title_with_volume_or_part / excerpted_work_titles / source_type /
publisher_or_journal / publication_year_or_date / issue_number on each row
during the inventory stage. This script only:
  1. honors already-filled fields (never overwrites them with regex guesses);
  2. falls back to regex extraction ONLY when a row leaves identity blank;
  3. de-duplicates rows into parent-source records by volume/issue/part;
  4. flags pollution (title == full footnote, 参见 residue, publisher/year
     mixed into title) so the model can repair the row on the next pass.

The regex fallback must NOT depend on the GB/T [X] type marker alone: many
journals omit it. It recognizes common Chinese patterns (书名号《》), the
"载《》"/"//" extracted-work marker, 转引自 chains, and foreign "Editor, ed.
Title" records, and otherwise reports 待人工补判 instead of guessing.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

TYPE_RE = re.compile(r'\[([A-Z])\]')
YEAR_RE = re.compile(r'(?:19|20)\d{2}(?:[\-－~～./年]\d{1,2}(?:[\-－~～./日]\d{1,2})?)?')
PREAMBLE_RE = re.compile(r'^.*?(?:阶段性成果[。；;]|项目[：:][^。；;]+[。；;])', re.S)
LEAD_RE = re.compile(r'^\s*(?:参见|见|参阅|转引自|载)\s*')
TYPE_NAMES = {'M':'图书','J':'期刊','N':'报纸','A':'档案','D':'学位论文','C':'论文集','S':'标准'}
PLACE_YEAR_PUB_RE = re.compile(
    r'(?:[^，。,；;]+?[：:]|[A-Z][A-Za-z\s]+?:)?'
    r'(?:19|20)\d{2}\s*[，,]?\s*$')
PUB_TAIL_RE = re.compile(
    r'(?:出版社|出版社\b|Press|University Press|Company|Inc\.|Ltd\.|书店|书局)?'
    r'[，,，]?\s*(?:19|20)\d{2}\s*[，,]?\s*(?:第?\s*[\d一二三四五六七八九十百]+\s*页?|[Pp]\.?\s*\d+[—-]?\d*)?\s*$')

def clean_note(value):
    value = re.sub(r'^\s*(?:[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|\[\d+\])\s*', '', str(value or ''))
    value = PREAMBLE_RE.sub('', value).strip()
    value = LEAD_RE.sub('', value).strip()
    return value

_AUTHOR_STRIP = re.compile(r'^(?:参见|见|参阅|转引自)\s*')


def clean_publisher(raw):
    """出版社/期刊名只保留名称本身：去'译者，译.'段、去'出版地：'前缀。
    例：'奚博铨，译.南京：江苏人民出版社'→'江苏人民出版社'；
    'New York: The Viking Press'→'The Viking Press'；'人民日报，1963-12-23（3）'→'人民日报'。"""
    s = str(raw or '').strip()
    if not s:
        return ''
    s = re.sub(r'^.*?译[.。]?\s*', '', s)
    s = re.sub(r'^[^，,;；]*?[：:]\s*', '', s)
    return s.strip(' .,，。;；:：')


def extract_author(note, title=''):
    """从脚注文本提取"待核单元"的责任者（作者/编者）段（模型未填时的兜底）。

    作者栏只允许出现待核单元本身的作者/编者：
    - 对析出结构（'析出作者.析出题名[M]//母文献编者.母文献题名'、'载《母文献》'
      或其它引用规范下的表示），析出作者与析出题名都不属于作者栏——只允许取
      析出标记之后（母文献段）内的编者作为作者栏内容。
    - 判定责任者必须有责任标记证据（国标 '作者.题名' 的点号、'作者：《题名》'
      的冒号、'Author, ed. Title'、'XXX编.母文献题名' 的编字系结尾）。
    - 仅以空格分隔的段（如 '全世界优秀青年代表 一致同情…[N]'）是标题内容，
      不构成作者。无法定位/证据不足时返回 ''（不臆造）。
    注意：析出标记因引用规范而异（//、载、in 等），本函数只处理常见形态；
    其余无法高置信识别的情形一律返回 ''，交由模型在盘点阶段填写。
    """
    n = re.sub(r'\s+', '', str(note or ''))
    t = re.sub(r'\s+', '', str(title or '')).lstrip('《')
    if not n or not t:
        return ''
    n2 = _AUTHOR_STRIP.sub('', n)
    n2 = re.sub(r'^本文系.*?。', '', n2)
    if not n2:
        return ''
    # 析出//母文献（[M]// 等）：责任者只从 // 之后（母文献段）取。
    # 注意：URL（https://）中也含 //，不能误切——只把"前一个字符是 ] 或 空白/句点
    # 之后的 //"当作析出标记，其余按 载 结构处理。
    if '//' in n2:
        # 优先匹配 [X]// 或 .// 或 空格// 形态；无此类形态时视为无析出标记
        m_cut = re.search(r'(?:\]|[\s。.》」』]|$)//', n2)
        if m_cut:
            n2 = n2[m_cut.end():]
    # 载《母文献》：从 载 之后开始取责任者
    m_zhai = re.search(r'载\s*《', n2)
    if m_zhai:
        n2 = n2[m_zhai.start():]
    # 候选责任者段：题名（或《 / 类型标记）之前的文本
    head = t[:10]
    pos = n2.find(head) if head else -1
    if pos <= 0:
        g = re.search(r'《', n2)
        pos = g.start() if g else -1
    if pos <= 0:
        m = re.search(r'\[[A-Za-z]\]', n2)
        pos = m.start() if m else -1
    if pos <= 0:
        return ''
    seg = n2[:pos]
    if not seg:
        return ''
    # 责任标记证据（由近及远）：1) 点号/冒号结尾的短责任段；
    # 2) 'XXX编/编著/主编/编集…' 编字系结尾（母文献编者/无署名编者）。
    lead = ''
    mm = re.search(r'(.+?)[.。:：]\s*$', seg)
    if mm:
        lead = mm.group(1)
    else:
        me = re.search(r'(.+?)(?:编著|编集|主编|编辑|编|整理|辑)$', seg)
        if me:
            lead = me.group(1) + me.group(2)
    if not lead:
        return ''
    lead = lead.strip(' .,，。;；:：-—–《》')
    m = re.search(r',?\s*ed\.?\s*$', lead, re.I)
    if m:
        lead = lead[:m.start()].strip()
    # 保守性检查：责任段内不允许再出现析出标记或类型标记；长度超过 40 视为抓取失败
    if '//' in lead or re.search(r'\[[A-Za-z]\]', lead):
        return ''
    if len(lead) > 40:
        return ''
    return lead

def split_zhaiyin(note):
    """转引自：'转引自' 之后才是可核对起点；之前的内容（源头文献）只作背景。

    若整条以 转引自 开头，clean_note 已剥掉引导词，直接返回整条。
    """
    idx = note.find('转引自')
    if idx >= 0:
        # 截取 转引自 之后；保留"转引自"所在片段之前的文字仅作 source_as_cited
        return note[idx + 3:].strip(), note[:idx].strip()
    return note, ''

def split_candidate(note):
    """Return (parent_candidate, excerpt_hint, cited_background).

    parent_candidate: text that should resolve to the parent work the user can
        supply (for 转引自 chains this is AFTER the last 转引自; for
        '析出//母文献' the parent is AFTER //).
    excerpt_hint: extracted-work title when '//' or '载' appears.
    """
    note = clean_note(note)
    # 转引自：取最后一段可核对起点
    while True:
        nxt, _bg = split_zhaiyin(note)
        if nxt and nxt != note:
            note = nxt
        else:
            break
    if '//' in note:
        left, right = note.split('//', 1)
        return right.strip(), left.strip(), ''
    m = re.search(r'(?:^|[。；;])\s*载\s*《([^》]+)》', note)
    if m:
        # 载《母文献》 形式：书名号内为母文献，前面的《析出》为析出
        parent = m.group(1).strip()
        excerpt = re.search(r'《([^》]+)》', note[:m.start()])
        return parent, (excerpt.group(1) if excerpt else ''), ''
    return note, '', ''

def parse_piece(piece):
    """Fallback parser. Never returns a fabricated confident title:
    returns {'parent_work_title_with_volume_or_part': '待人工补判', ...} when it
    cannot recognize the structure with high confidence.
    """
    def _record(title, source_type='未确定', excerpt='', publisher='', year='', issue=''):
        title = (title or '').strip(' .,，。;；:：')
        display = title or '待人工补判'
        identity = re.sub(r'\s+', '', display)
        key = 'ML-' + hashlib.sha1(identity.encode('utf-8')).hexdigest()[:10]
        return {'source_key': key,
                'parent_work_title_with_volume_or_part': display,
                'excerpted_work_titles': excerpt,
                'author_editor': '', 'source_type': source_type,
                'publisher_or_journal': publisher,
                'publication_year_or_date': year, 'issue_number': issue,
                'material_status': 'missing', 'material_route': '', 'material_path': '',
                'next_action': '人工确认母文献身份后核对' if not title else '补充完整母文献后核对',
                'source_as_cited': piece}

    piece = str(piece or '').strip(' .,，。;；:：')
    if not piece:
        return _record('')

    # 1) GB/T [X] 标记可用：保留原有高置信逻辑
    match = TYPE_RE.search(piece)
    if match:
        source_type = TYPE_NAMES.get(match.group(1), '未确定')
        before = piece[:match.start()].strip(' .。;；')
        after = piece[match.end():].strip(' .。;；')
        title = before
        if re.search(r'[。.]', before):
            title = re.split(r'[。.]\s*', before, maxsplit=1)[1].strip()
        elif '. ' in before:
            title = before.split('. ', 1)[1].strip()
        elif ', ' in before:
            title = before.split(', ', 1)[1].strip()
        year = YEAR_RE.search(after)
        publication_year_or_date = year.group(0) if year else ''
        publisher = ''
        if after:
            publisher = after[:year.start()].strip(' ，,：:') if year else after
        issue = ''
        issue_match = re.search(r'[（(]?(\d{1,3})[）)]', after)
        if source_type in ('报纸','期刊') and issue_match:
            issue = issue_match.group(1)
        if not title or len(title) < 2:
            title = '待人工补判'
        rec = _record(title, source_type, publisher=publisher, year=publication_year_or_date, issue=issue)
        rec['next_action'] = '人工确认母文献身份后核对' if title == '待人工补判' else rec['next_action']
        return rec

    # 2) 无 [X]：书名号《》策略。多个《》时，最末一个通常是母文献
    #    （报纸名/书名），其前的《》多为析出文章题名 → 填入 excerpted。
    hits = re.findall(r'《([^》]{2,})》', piece)
    if hits:
        parent_title = hits[-1].strip()
        excerpt = ''
        if len(hits) >= 2:
            # 收集除最后一个外的书名号作为析出候选（逗号分隔多个文章）
            excerpt = '、'.join(h.strip() for h in hits[:-1])
        return _record(parent_title, '未确定', excerpt=excerpt)

    # 3) 外文 "Author, Title" / "Editor, ed. Title[: Subtitle]. Place: Publisher, Year" 无 [X]
    #    取 ed. 之后、出版信息（最后一个 xxx Press / University Press / 出版社）之前的部分为题名。
    if re.search(r'\bed\.\s+', piece):
        tail = piece.split('ed.', 1)[1].strip()
        cut = -1
        for kw in ('University Press', '出版社', 'Press'):
            i = tail.rfind(kw)
            if i >= 0:
                seg = tail[:i]
                # 回退到最近的句子/分句边界（. 或 , 后），避免吞入出版地
                for sep in ('. ', '。', ', ', '，'):
                    j = seg.rfind(sep)
                    if j >= 0:
                        cut = j + len(sep)
                        break
                if cut < 0:
                    cut = 0
                break
        if cut >= 0:
            title = tail[:cut].strip(' .,，。;；:：')
        else:
            # 没有出版社标志：到年份/句点截断，避免吞入页码链
            m_year = re.search(r'(?:19|20)\d{2}\s*[:.]', tail)
            title = (tail[:m_year.start()] if m_year else tail).strip(' .,，。;；:：')
        if title:
            return _record(title, '图书')
        return _record('', '图书')

    # 4) 常见 "作者：《题名》，..." / "作者：《题名》//..." 已被书名号捕获；其余无法高置信识别
    return _record('', '')

def suspicious_title(title, note=''):
    title = str(title or '').strip()
    compact = re.sub(r'\s+', '', title)
    note_compact = re.sub(r'\s+', '', str(note or ''))
    if (not title or title in ('待归一化','待核实','待人工补判')):
        return True
    if '本文系' in title or '阶段性成果' in title or '参见' in title or '转引自' in title:
        return True
    if bool(re.search(r'\[[MJNADCS]\]', title)) or '//' in title:
        return True
    if (note_compact and compact == note_compact):
        return True
    if YEAR_RE.search(title) or re.search(r'出版社|人民出版社|University Press|Press\b', title):
        return True
    return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('records', type=Path)
    args = ap.parse_args()
    raw = json.loads(args.records.read_text(encoding='utf-8'))
    # Schema tolerance: accept a bare list of footnote rows and wrap it.
    if isinstance(raw, list):
        data = {'footnotes': raw, 'sources': [], 'status': '初步盘点结果'}
    else:
        data = raw if isinstance(raw, dict) else {'footnotes': [], 'sources': [], 'status': '初步盘点结果'}
    rows = data.get('footnotes')
    if not isinstance(rows, list) or not rows:
        raise SystemExit('BLOCKED: canonical footnotes list is missing or empty')
    grouped = {}
    for row in rows:
        note = row.get('footnote_content') or row.get('footnote_text') or ''
        current_title = str(row.get('parent_work_title_with_volume_or_part') or '').strip()
        excerpt = str(row.get('excerpted_work_titles') or '').strip()
        # 职责分工：优先采用行内已填字段；空白时才走兜底解析
        if current_title and not suspicious_title(current_title, note):
            title = current_title
            source_type = str(row.get('source_type') or '未确定')
            publisher = str(row.get('publisher_or_journal') or '')
            year = str(row.get('publication_year_or_date') or '')
            issue = str(row.get('issue_number') or '')
            rec = {'source_key': '', 'parent_work_title_with_volume_or_part': title,
                   'excerpted_work_titles': excerpt, 'author_editor': '',
                   'source_type': source_type, 'publisher_or_journal': publisher,
                   'publication_year_or_date': year, 'issue_number': issue,
                   'material_status': 'missing', 'material_route': '', 'material_path': '',
                   'next_action': '补充完整母文献后核对', 'source_as_cited': note}
        else:
            parent_cand, excerpt_hint, _bg = split_candidate(note)
            rec = parse_piece(parent_cand)
            if excerpt_hint and not excerpt:
                rec['excerpted_work_titles'] = excerpt_hint
        key = row.get('source_key') or ''
        if not key or key in ('待归一化','待核实','待人工补判'):
            key = rec.get('source_key') or ''
        rec['source_key'] = key
        # 归并：同一母文献（按题名+卷册归一化 key）聚为一条
        identity = re.sub(r'\s+', '', rec.get('parent_work_title_with_volume_or_part') or '') or '待人工补判'
        merge_key = key if key and key not in ('待归一化','待核实','待人工补判') else 'ML-' + hashlib.sha1(identity.encode('utf-8')).hexdigest()[:10]
        item = grouped.setdefault(merge_key, rec)
        if item.get('parent_work_title_with_volume_or_part', '待人工补判') in ('待归一化','待核实','待人工补判') and \
           rec.get('parent_work_title_with_volume_or_part') not in ('待归一化','待核实','待人工补判'):
            item['parent_work_title_with_volume_or_part'] = rec['parent_work_title_with_volume_or_part']
        page = row.get('page', '')
        marker = row.get('marker', '')
        loc = f'第{page}页脚注{marker}' if str(page) and str(marker) else f'页{page}注{marker}'
        item.setdefault('_locations', []).append(loc)
        for field in ('source_type','publisher_or_journal','publication_year_or_date','issue_number','excerpted_work_titles'):
            if not item.get(field) and rec.get(field):
                item[field] = rec[field]
        # 材料状态聚合：任一脚注已提供材料 → 整条 source 视为已提供
        if not item.get('_mat') and str(row.get('source_file_status') or row.get('material_status') or '') == 'already supplied':
            item['_mat'] = 'already supplied'
            item.setdefault('_route', str(row.get('material_route') or ''))
            item.setdefault('_path', str(row.get('material_path') or ''))
        row['source_key'] = merge_key
        row['parent_work_title_with_volume_or_part'] = item.get('parent_work_title_with_volume_or_part', '待人工补判')
        row['source_type'] = item.get('source_type', '未确定')
        row['publisher_or_journal'] = item.get('publisher_or_journal', '')
        row['publication_year_or_date'] = item.get('publication_year_or_date', '')
        row['issue_number'] = item.get('issue_number', '')
        row['author_editor'] = item.get('author_editor', '')
    sources = []
    for item in grouped.values():
        item['footnote_locations'] = '；'.join(item.pop('_locations', []))
        if not item.get('author_editor'):
            item['author_editor'] = extract_author(str(item.get('source_as_cited') or ''),
                                                   str(item.get('parent_work_title_with_volume_or_part') or ''))
            # 题名隐含人名（人名+选集/文集/文选/全集，人名限 2-4 字）→ 人名即作者
            if not item.get('author_editor'):
                tflat = re.sub(r'\s+', '', str(item.get('parent_work_title_with_volume_or_part') or '')).lstrip('《')
                m_name = re.match(r'^([\u4e00-\u9fff·]{2,4})(?:选集|文集|全集|文选)', tflat)
                if m_name:
                    item['author_editor'] = m_name.group(1)
        # 出版社/期刊名只保留名称：去"译者译。"段与"出版地："前缀（北京：/New York:）
        item['publisher_or_journal'] = clean_publisher(item.get('publisher_or_journal'))
        if item.pop('_mat', None):
            item['material_status'] = 'already supplied'
            item['material_route'] = item.get('_route', '')
            item['material_path'] = item.get('_path', '')
            item.pop('_route', None)
            item.pop('_path', None)
        sources.append(item)
    data['sources'] = sources
    data.setdefault('audit_meta', {})['source_normalization'] = 'academic-footnote-audit/scripts/normalize_sources.py'
    args.records.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
    print(f'PASS: normalized {len(rows)} footnotes into {len(sources)} explicit parent sources')

if __name__ == '__main__':
    main()
