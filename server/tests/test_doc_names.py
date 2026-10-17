"""文档一致性：docs/server-invariants.md 里反引号引用的每个测试名都必须
真实存在于 tests/ 下（文档与测试套件不允许漂移）。"""
import ast
import pathlib
import re


def _doc_cited_test_names(doc: pathlib.Path) -> set[str]:
    text = doc.read_text(encoding="utf-8")
    return set(re.findall(r"`(test_[a-zA-Z0-9_]+)`", text))


def _defined_test_names(tests_dir: pathlib.Path) -> set[str]:
    names: set[str] = set()
    for f in tests_dir.glob("test_*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                names.add(node.name)
    return names


def test_invariants_doc_test_names_exist():
    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    doc = repo_root / "docs" / "server-invariants.md"
    assert doc.exists(), f"文档不存在：{doc}"
    cited = _doc_cited_test_names(doc)
    assert cited, "文档中应至少引用一个测试名（解析失败？）"
    defined = _defined_test_names(pathlib.Path(__file__).resolve().parent)
    missing = sorted(cited - defined)
    assert not missing, f"文档引用了不存在的测试名：{missing}"
