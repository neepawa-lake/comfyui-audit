#!/usr/bin/env python3
"""scan_sinks.py — ComfyUI custom_nodes 代码执行/命令执行 sink 扫描器。

用法: python3 scan_sinks.py <custom_nodes目录> [--all]

优先级:
  A = sink 位于已注册节点类内, 且该类有 STRING 输入  -> 污点可能直达, 优先人工审
  B = sink 位于已注册节点类内, 但无直接 STRING 输入
  C = sink 在普通模块/工具函数里(含 vendored 库)     -> 看调用链是否被节点触发
  --all 显示全部; 默认只显示 A/B
"""
import ast
import os
import re
import sys

SINK_NAMES = {"eval", "exec", "compile", "__import__"}          # Call func 是裸 Name
SINK_ATTRS = {                                                   # obj.method(...)
    ("os", "system"), ("os", "popen"), ("os", "exec"), ("os", "execv"),
    ("os", "execve"), ("os", "spawnl"), ("os", "spawnp"), ("os", "fork"),
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
    ("subprocess", "check_call"), ("subprocess", "check_output"),
    ("subprocess", "getoutput"), ("subprocess", "getstatusoutput"),
    ("pty", "spawn"), ("pickle", "loads"), ("pickle", "load"),
    ("marshal", "loads"), ("importlib", "import_module"),
    ("importlib", "exec_module"),
}

MAPPING_RE = re.compile(r'["\']([A-Za-z0-9_]+)["\']\s*:\s*([A-Za-z0-9_]+)\s*[,}]')


def file_registered_classes(path, src):
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):   # ValueError: 空字节等, SyntaxError: 语法错
        return set()
    if "NODE_CLASS_MAPPINGS" not in src:
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == "NODE_CLASS_MAPPINGS":
                    names |= {v for _, v in MAPPING_RE.findall(ast.get_source_segment(src, node) or "")}
    return names


def sink_of(call):
    f = call.func
    if isinstance(f, ast.Name) and f.id in SINK_NAMES:
        return f.id
    if isinstance(f, ast.Attribute):
        if isinstance(f.value, ast.Name) and (f.value.id, f.attr) in SINK_ATTRS:
            return f"{f.value.id}.{f.attr}"
    for kw in call.keywords:                       # 任何调用带 shell=True 都算
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
            return "shell=True"
    return None


def scan_file(path):
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        print(f"[skip] 无法读取: {path}", file=sys.stderr)
        return []
    if "\x00" in src:                 # 二进制/改名文件: ast.parse 不接受空字节
        print(f"[skip] 含空字节(疑似二进制): {path}", file=sys.stderr)
        return []
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        print(f"[skip] 解析失败: {path}", file=sys.stderr)
        return []
    registered = file_registered_classes(path, src)
    hits = []
    def visit(node, cls_name, func_name):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name, None)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, cls_name, child.name)
            else:
                if isinstance(child, ast.Call):
                    s = sink_of(child)
                    if s:
                        hits.append((child.lineno, s, cls_name, func_name))
                visit(child, cls_name, func_name)
    visit(tree, None, None)

    results = []
    for lineno, sink, cls, func in hits:
        prio, has_string = "C", False
        if cls:
            m = re.search(rf"^class {re.escape(cls)}\b.*?(?=^class |\Z)", src, re.S | re.M)
            seg = m.group(0) if m else ""
            has_string = "STRING" in seg and "INPUT_TYPES" in seg
            if cls in registered:
                prio = "A" if has_string else "B"
        results.append((prio, lineno, sink, cls or "-", func or "-", has_string))
    return results


def main():
    root = sys.argv[1]
    show_all = "--all" in sys.argv
    summary = {"A": [], "B": [], "C": []}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in
                       ("__pycache__", "node_modules", ".git", "web", "js", "venv", ".venv")]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            for prio, lineno, sink, cls, func, has_str in scan_file(p):
                summary[prio].append(f"{rel}:{lineno}  [{sink}]  class={cls}  func={func}"
                                     + ("  <STRING input>" if has_str else ""))
    for prio in "ABC":
        rows = summary[prio]
        print(f"\n===== Priority {prio}  ({len(rows)} hits) =====")
        if prio == "C" and not show_all:
            print("  (省略, 加 --all 查看; 多为 vendored 库/工具函数)")
            continue
        for r in rows:
            print("  " + r)


if __name__ == "__main__":
    main()
