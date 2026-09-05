# -*- coding: utf-8 -*-
"""verify_source_quotes.py — 来源引句核对辅助工具（技能可选工具，不参与主流水线）

用途：在来源 PDF 中核对论文脚注的引句是否逐字命中原文。
解决核对中最耗时的三类问题：
  1. 印刷页码 -> 电子页偏移因卷/版本而异（自动多数投票探测）；
  2. OCR 文本层混入页码行（"- 43 -"、"-4 3 -"）与书眉行，干扰逐字匹配（自动剔除）；
  3. 引句跨页断行（页尾断词、下页续行）（自动拼接相邻页）。

用法：
  1) 探测偏移：    python verify_source_quotes.py <pdf> --detect-offset
  2) 核对引句：    python verify_source_quotes.py <pdf> --offset 15 --pages 43-44,55 \
                       --quotes "句一" --quotes "句二"
  3) 自动偏移核对：python verify_source_quotes.py <pdf> --pages 43-44 --quotes "句一"
                   (不给 --offset 时自动探测)

输出：每条引句一行 —— ✓ 命中(印刷页 x[-y]，剔除噪声说明) 或 ✗ 未命中(给出最近位置窗口供人工复核)。
设计哲学：未命中只报告位置与窗口，不做"作者笔误"类结论，由用户定夺。
依赖：pdfplumber（核对环境已具备）。纯标准库 + pdfplumber，无其他依赖。
"""

import argparse
import re
import sys
from collections import Counter


# ---------- 页码行 / 书眉 识别 ----------

_PAGE_NO_RE = re.compile(r'^[\-\—–−]?\d{1,4}[\-\—–−]?$')


def is_page_no_line(line: str) -> bool:
    """整行是页码：去空白后为纯数字（1-4位），容忍 -43-、-4 3 - 等 OCR 形态。"""
    s = re.sub(r'\s', '', line)
    return bool(s) and bool(_PAGE_NO_RE.match(s))


def gbk_fix(text: str) -> str:
    """部分中文 PDF 把文本层按 GBK 编码、却以 Latin-1 输出成乱码（如 'µÚ Ò»' 实为 GBK '第一'）。
    去掉空白后 latin-1→gbk 反转还原；失败时原样返回。整行应用（页码行同样受益）。"""
    flat = re.sub(r'\s+', '', text)
    try:
        return flat.encode('latin-1', errors='ignore').decode('gbk', errors='replace')
    except Exception:
        return text


_FW_DIGITS = str.maketrans('０１２３４５６７８９－—', '0123456789--')


def to_ascii_digits(s: str) -> str:
    """全角数字/横线转半角（部分书眉页码用全角，如 '５１ 毛泽东选集'）。"""
    return s.translate(_FW_DIGITS)


def detect_headers(lines_by_page, min_len=8):
    """自动识别书眉：在相邻多页顶部重复出现的行。
    lines_by_page: list of list[str]（每页已剔页码行的行序列）
    返回被判定为书眉的规范化行集合。
    """
    tops = []
    for lines in lines_by_page:
        for ln in lines[:2]:
            c = re.sub(r'\s+', '', ln)
            if len(c) >= min_len:
                tops.append(c)
    cnt = Counter(tops)
    n_pages = len(lines_by_page)
    headers = {t for t, c in cnt.items() if c >= 2 and c >= max(2, n_pages * 0.5)}
    return headers


def clean_page_lines(lines, headers):
    """剔页码行与书眉行，返回正文行（保留原顺序）。"""
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if is_page_no_line(s):
            continue
        if re.sub(r'\s+', '', s) in headers:
            continue
        out.append(s)
    return out


def body_lines(pdf, elec_idx, headers, mojibake=False):
    """返回第 elec_idx 页（0-based）剔页码/书眉后的正文行列表（未去空白）。
    mojibake=True 时先做 GBK 反转修复（latin-1→gbk）。"""
    raw = [l for l in (pdf.pages[elec_idx].extract_text() or '').split('\n')]
    if mojibake:
        raw = [gbk_fix(l) for l in raw]
    return clean_page_lines(raw, headers)


# ---------- 偏移探测 ----------

def detect_offset(pdf, window=40, mojibake=False):
    """多数投票：扫若干页的页尾数字行，offset = 电子页号 - 印刷页码。"""
    n = len(pdf.pages)
    if n <= 5:
        return None
    step = max(1, n // window)
    votes = []
    for i in range(3, n, step):  # 跳过前几页（版权/目录）
        lines = [l for l in (pdf.pages[i].extract_text() or '').split('\n') if l.strip()]
        if not lines:
            continue
        if mojibake:
            lines = [gbk_fix(l) for l in lines]
        # 页码常见形态：独立数字行(- 43 -)；页首/页尾书眉行行首或行尾数字（'５１ 毛泽东选集…'、'…考察报告１９'）
        cands = []
        for ln in (lines[-1], lines[0]):
            s = re.sub(r'\s', '', to_ascii_digits(ln))
            if s and re.fullmatch(r'[\-\—–−]?\d{1,4}[\-\—–−]?', s):
                cands.append(int(re.sub(r'\D', '', s)))
                continue
            has_cjk = bool(re.search(r'[\u4e00-\u9fff]', s))
            if not has_cjk:
                continue
            m_head = re.match(r'[\-\—–−]?(\d{1,4})', s)
            m_tail = re.search(r'(\d{1,4})[\-\—–−]?$', s)
            if m_head:
                cands.append(int(m_head.group(1)))
            if m_tail and m_tail.start() > 0:
                cands.append(int(m_tail.group(1)))
        for num in cands:
            votes.append((i + 1) - num)
    if not votes:
        return None
    off, freq = Counter(votes).most_common(1)[0]
    return off, freq, len(votes)


# ---------- 引句核对 ----------

def parse_pages(spec):
    """'43-44,55' -> [43,44,55]"""
    pages = []
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-', 1)
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return pages


def match_report(target, combo):
    """在 combo(去空白) 中找 target(去空白)。命中返回 (True, 位置)；否则 (False, 最近位置)。"""
    t = re.sub(r'\s+', '', target)
    if t in combo:
        return True, combo.find(t)
    # 未命中：取目标前 8 字找最近出现位置，返回窗口起点
    probe = t[:8]
    pos = combo.find(probe)
    return False, (pos if pos >= 0 else -1)


def verify(pdf_path, offset, printed_pages, targets, mojibake=False):
    results = []
    pdf = pdfplumber_open(pdf_path)
    n = len(pdf.pages)

    # 电子页窗口：核对页 ±12 页（供书眉频率统计，比核对页范围宽得多）
    e_start = max(1, min(printed_pages) + offset - 12)
    e_end = min(n, max(printed_pages) + offset + 12)
    raw = []
    for i in range(e_start - 1, e_end):
        lines = [l for l in (pdf.pages[i].extract_text() or '').split('\n')]
        raw.append([gbk_fix(l) for l in lines] if mojibake else lines)
    headers = detect_headers(raw)

    # 每印刷页的正文行（剔页码行 + 统计书眉；顶部残留书眉由降级重试兜底）
    body_lines_by_print = {}
    for pp in printed_pages:
        ei = pp + offset  # 1-based 电子页；印刷页码 = 电子页号 − offset
        if 1 <= ei <= n:
            body_lines_by_print[pp] = body_lines(pdf, ei - 1, headers, mojibake)

    def text_of(pp, skip_head=0):
        """印刷页 pp 的正文（去空白），跳过顶部 skip_head 行（用于剔除书眉降级）。"""
        lines = body_lines_by_print.get(pp, [])
        return re.sub(r'\s+', '', ''.join(lines[skip_head:]))

    for t in targets:
        hit = False
        detail = ''
        cands = []  # (说明, 文本)
        for pp in printed_pages:
            cands.append((f'印刷页{pp}', text_of(pp)))
        for a in range(len(printed_pages) - 1):
            p1, p2 = printed_pages[a], printed_pages[a + 1]
            if p1 not in body_lines_by_print or p2 not in body_lines_by_print:
                continue
            # 跨页拼接：第二页顶部书眉可能打断断行句 → 尝试剔除顶部 0/1/2 行
            for skip in (0, 1, 2):
                label = f'印刷页{p1}-{p2}(跨页)' + (f',剔{p2}页顶{skip}行' if skip else '')
                cands.append((label, text_of(p1, 0) + text_of(p2, skip)))
        for label, text in cands:
            tn = re.sub(r'\s+', '', t)
            if tn and tn in text:
                hit = True
                detail = label + ('[自动剔书眉/页码噪声]' if headers else '')
                break
        if hit:
            results.append((t, True, detail))
        else:
            # 未命中：报最近位置窗口
            allc = ''.join(text_of(pp) for pp in sorted(body_lines_by_print))
            probe = re.sub(r'\s+', '', t)[:8]
            pos = allc.find(probe) if probe else -1
            win = allc[max(0, pos - 25):pos + 40] if pos >= 0 else ''
            results.append((t, False, win))
    return results, headers


def _ensure_pdfplumber():
    """依赖自检：pdfplumber 缺失时自动 pip 安装一次；仍失败给出指引后退出。"""
    try:
        import pdfplumber
        return pdfplumber
    except ImportError:
        pass
    print('[依赖自检] 未检测到 pdfplumber，尝试自动安装 …', file=sys.stderr)
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', 'pdfplumber',
             '--quiet', '--disable-pip-version-check'],
            capture_output=True, text=True, timeout=240)
        if r.returncode == 0:
            try:
                import pdfplumber  # 再次导入（pip 已装好）
                print('[依赖自检] pdfplumber 安装成功，继续执行', file=sys.stderr)
                return pdfplumber
            except ImportError:
                pass
        err = (r.stderr or r.stdout or '').strip()[-300:]
    except Exception as e:
        err = str(e)
    sys.exit('pdfplumber 缺失且自动安装失败：%s\n'
             '手动安装：python -m pip install pdfplumber（纯 pip 包，无编译）\n'
             '无库替代路径：用 pdftoppm 渲染页面后由多模态模型直接看图核对（见 sources.md 视觉核对节）。' % err)


def pdfplumber_open(path):
    pdfplumber = _ensure_pdfplumber()
    return pdfplumber.open(path)


# ---------- 主入口 ----------

def main():
    ap = argparse.ArgumentParser(description='来源引句核对：偏移探测 + 逐字命中验证（容忍页码/书眉噪声与跨页断行）')
    ap.add_argument('pdf', help='来源 PDF 路径')
    ap.add_argument('--detect-offset', action='store_true', help='只探测印刷页码偏移并退出')
    ap.add_argument('--offset', type=int, default=None, help='印刷页码 = 电子页 − offset（缺省自动探测）')
    ap.add_argument('--pages', default='', help='印刷页码范围，如 43-44,55')
    ap.add_argument('--quotes', action='append', default=[], help='目标引句（可多次）')
    ap.add_argument('--mojibake-gbk', action='store_true',
                    help='文本层是 GBK 误读为 Latin-1 的乱码时开启（latin-1→gbk 反转还原，见 gbk_fix）')
    args = ap.parse_args()

    pdf = pdfplumber_open(args.pdf)
    offset = args.offset
    if offset is None:
        det = detect_offset(pdf, mojibake=args.mojibake_gbk)
        if det is None:
            sys.exit('无法自动探测页码偏移，请用 --offset 手工指定（先看几页页尾印刷页码）')
        offset, freq, total = det
        print(f'[偏移] 自动探测: 印刷页码 = 电子页 − {offset}（{freq}/{total} 页投票）')

    if args.detect_offset:
        return
    if not args.pages or not args.quotes:
        sys.exit('核对模式需 --pages 与 --quotes（可多次）')

    pages = parse_pages(args.pages)
    src = 'GBK修复' if args.mojibake_gbk else '文本层'
    print(f'[来源] {src}')
    results, headers = verify(args.pdf, offset, pages, args.quotes, mojibake=args.mojibake_gbk)

    print(f'[窗口] 印刷页 {min(pages)}-{max(pages)}（电子页 {min(pages)+offset}-{max(pages)+offset}）')
    if headers:
        print(f'[清洗] 自动识别书眉 {len(headers)} 种，页码行全部剔除')
    for t, ok, info in results:
        short = t if len(t) <= 30 else t[:30] + '…'
        if ok:
            print(f'  ✓ 命中 | {short} | {info}')
        else:
            print(f'  ✗ 未命中 | {short}')
            if info:
                print(f'      最近位置窗口: …{info}…')
            else:
                print(f'      目标句前段在窗口内未出现，建议人工查原书或核对标称页码')
    pdf.close()


if __name__ == '__main__':
    main()
