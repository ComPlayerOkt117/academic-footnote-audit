#!/usr/bin/env python3
"""Single controlled entry point for canonical audit output finalization."""
import argparse
import json
import runpy
import sys
from pathlib import Path

CODE_SUFFIXES = {'.py', '.pyw', '.ps1', '.bat', '.cmd', '.js', '.mjs', '.cjs', '.vbs', '.sh', '.exe', '.dll'}

def is_audit_root(path):
    if not path.is_dir() or not (path / '00_论文原稿').is_dir():
        return False
    if not (path / '04_工作文件' / 'canonical_records.json').is_file():
        return False
    manuscript_files = list(path.glob('*.docx')) + list(path.glob('*.pdf'))
    if not manuscript_files:
        manuscript_files = list((path / '00_论文原稿').glob('*.docx')) + list((path / '00_论文原稿').glob('*.pdf'))
    return bool(manuscript_files)

def run_stage(script, *arguments):
    """Run a fixed stage in-process so Unicode Windows paths stay intact."""
    old_argv = sys.argv
    try:
        sys.argv = [str(script), *(str(arg) for arg in arguments)]
        runpy.run_path(str(script), run_name='__main__')
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code:
            raise RuntimeError(f'fixed stage failed: {script} ({code})') from exc
    finally:
        sys.argv = old_argv

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('audit_root', type=Path)
    ap.add_argument('records', type=Path, nargs='?')
    args = ap.parse_args()
    supplied_root = args.audit_root.resolve()
    # Windows PowerShell can damage non-ASCII command-line arguments before
    # Python receives them.  If the supplied path does not exist, recover only
    # from a nearby directory that already has the canonical audit structure;
    # never guess from the whole disk.
    if not is_audit_root(supplied_root):
        nearby = []
        parent = supplied_root.parent
        if parent.is_dir():
            for child in parent.iterdir():
                if is_audit_root(child):
                    nearby.append(child.resolve())
        if len(nearby) == 1:
            supplied_root = nearby[0]
        else:
            raise SystemExit('BLOCKED: audit project path is unavailable or ambiguous')
    candidates = [supplied_root]
    if (supplied_root / '脚注核对').is_dir(): candidates.append(supplied_root / '脚注核对')
    if args.records:
        rec = args.records.resolve()
        if rec.name == 'canonical_records.json' and rec.parent.name == '04_工作文件': candidates.append(rec.parent.parent)
    roots = [p for p in candidates if (p / '00_论文原稿').is_dir()]
    root = (roots[0] if roots else supplied_root).resolve()
    skill = Path(__file__).resolve().parent.parent
    records = root / '04_工作文件' / 'canonical_records.json'
    if not records.exists() and args.records and args.records.resolve().exists():
        records.parent.mkdir(parents=True, exist_ok=True)
        records.write_bytes(args.records.resolve().read_bytes())
    if not records.exists():
        raise SystemExit('BLOCKED: canonical records do not exist')
    output_dir = root / '03_核对记录'
    violations = [str(p) for p in root.rglob('*') if p.is_file() and p.suffix.lower() in CODE_SUFFIXES]
    # 一次性工具脚本允许保存在 04_工作文件 并仅产出中间记录；正式交付可信度由
    # 统一渲染器凭据 + 输出闸门哈希校验承担。不再因项目内存在脚本就整份阻断，
    # 以免模型为对齐格式自建脚本时触发技能自身禁令、陷入死循环。
    work_scripts = [p for p in violations if p.replace('\\', '/').find('/04_工作文件/') >= 0]
    other_code = [p for p in violations if p not in work_scripts]
    violation_file = root / '04_工作文件' / '流程违规记录.json'
    violation_file.parent.mkdir(parents=True, exist_ok=True)
    violation_file.write_text(json.dumps({
        'status': '已记录（不阻断）',
        'work_dir_tool_scripts': work_scripts,
        'other_code_files': other_code,
        'rule': '04_工作文件 中的一次性工具脚本允许存在但只产出中间记录；正式输出必须由统一渲染器生成并通过输出闸门（凭据+哈希）。其他位置的代码文件需在交付报告中说明用途。'
    }, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
    # Remove only the four canonical user-facing outputs.  This prevents a
    # prior interrupted run from contaminating row-count/hash validation while
    # preserving all evidence and intermediate records.
    for name in ('脚注逐条清单.html','脚注逐条清单.csv','待核文献准备清单.html','待核文献准备清单.csv'):
        target = output_dir / name
        if target.exists():
            target.unlink()
    renderer = skill / 'scripts' / 'render_audit_html.py'
    normalizer = skill / 'scripts' / 'normalize_sources.py'
    gate = skill / 'scripts' / 'validate_audit_outputs.py'
    run_stage(normalizer, records)
    run_stage(renderer, records, output_dir)
    run_stage(gate, root, skill)
    boundary_report = root / '04_工作文件' / '边界检查报告.json'
    pipeline_status = 'passed'
    if boundary_report.exists():
        boundary = json.loads(boundary_report.read_text(encoding='utf-8'))
        if boundary.get('status') == 'review':
            pipeline_status = 'passed_with_review'
    receipt = root / '04_工作文件' / '流水线完成记录.json'
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        'entrypoint': str(Path(__file__).resolve()),
        'records': str(records),
        'output_dir': str(output_dir),
        'status': pipeline_status,
        'protocol_violations': violations,
        'outputs_verified_by': str(gate.resolve()),
        'completion_rule': '仅在统一渲染器生成且输出验收通过后写入本记录'
    }, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
    print('PASS: audit pipeline completed and output gate passed')

if __name__ == '__main__':
    main()
