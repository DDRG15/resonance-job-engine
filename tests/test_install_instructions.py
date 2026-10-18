"""A stranger must be able to install and run the project from the README alone.

These checks pin the install contract so it cannot drift silently:

* ``requirements.txt`` holds what the app needs to run, every package pinned with
  ``==``. An unpinned or missing package means a fresh install resolves a different
  tree than the one the suite ran against.
* ``requirements-dev.txt`` starts with ``-r requirements.txt``, so one command installs
  runtime plus test tools, and test tools never ship in the Docker image.
* Every third-party package the shipped code imports is declared in ``requirements.txt``
  (``desktop/bandeja.py`` imports ``pystray`` and ``PIL``; both were missing).
* The README states the Python version, a virtual environment and both install commands.
"""

from __future__ import annotations

import ast
import importlib.metadata as md
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_TOOLS = {"pytest", "pytest-asyncio", "pandas"}
SKIP_DIRS = {".venv", "venv", "build", "dist", "node_modules", "frontend", "__pycache__",
             ".git", "data", "backups", "cookies", "credentials", "tests"}


def requirement_lines(name: str) -> list[str]:
    lines = []
    for raw in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower().strip()


def pinned_names(name: str) -> set[str]:
    names = set()
    for line in requirement_lines(name):
        if line.startswith("-r"):
            continue
        names.add(canonical(re.split(r"[=\[;]", line, 1)[0]))
    return names


def project_python_files() -> list[Path]:
    """Every .py the project owns. Hidden folders, virtual environments (any name, found
    by `pyvenv.cfg`), `site-packages` and build output are pruned: scanning a venv would
    report the imports of third-party packages as if the project made them."""
    found = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and d not in {"node_modules", "build", "dist",
                                                          "__pycache__", "site-packages"}
                   and not (Path(folder) / d / "pyvenv.cfg").exists()]
        found.extend(Path(folder) / f for f in files if f.endswith(".py"))
    return found


def source_files(include_tests: bool) -> list[Path]:
    found = []
    for path in project_python_files():
        parts = set(path.relative_to(ROOT).parts[:-1])
        if include_tests:
            if "tests" in parts:
                found.append(path)
        elif not parts & SKIP_DIRS:
            found.append(path)
    return found


def local_names() -> set[str]:
    names = {"tests"}
    for path in project_python_files():
        rel = path.relative_to(ROOT)
        names.add(path.stem)
        if len(rel.parts) > 1:
            names.add(rel.parts[0])
    return names


def third_party_imports(paths) -> set[str]:
    stdlib = set(sys.stdlib_module_names)
    local = local_names()
    found = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top not in stdlib and top not in local:
                    found.add(top)
    return found


def distributions_for(modules: set[str]) -> dict[str, set[str]]:
    mapping = md.packages_distributions()
    return {m: {canonical(d) for d in mapping.get(m, [])} for m in modules}


def test_dev_requirements_include_runtime_first():
    assert requirement_lines("requirements-dev.txt")[0] == "-r requirements.txt"


def test_every_requirement_is_pinned_exactly():
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in requirement_lines(name):
            if line.startswith("-r"):
                continue
            pinned = r"[A-Za-z0-9_.\-]+(\[[a-z,]+\])?==[A-Za-z0-9_.\-]+"
            marker = r"(\s*;\s*python_version\s*[<>]=?\s*\"3\.\d+\")?"
            assert re.fullmatch(pinned + marker, line), (name, line)


def test_runtime_requirements_carry_no_test_tools():
    assert not (pinned_names("requirements.txt") & TEST_TOOLS)


def test_no_package_is_pinned_twice():
    assert not (pinned_names("requirements.txt") & pinned_names("requirements-dev.txt"))


def test_shipped_imports_are_covered_by_requirements_txt():
    runtime = pinned_names("requirements.txt")
    for module, dists in distributions_for(third_party_imports(source_files(False))).items():
        assert dists, f"{module} is imported by shipped code but is not installed"
        assert dists & runtime, f"{module} ({sorted(dists)}) is missing from requirements.txt"


def exact_dependencies(names: set[str]) -> set[str]:
    """Distributions that a declared package pins with `==` (e.g. pydantic pins
    pydantic-core). Declaring them again would only let the two pins drift apart."""
    found = set()
    for name in names:
        try:
            requires = md.requires(name) or []
        except md.PackageNotFoundError:
            continue
        for requirement in requires:
            if "extra ==" in requirement:
                continue
            match = re.match(r"([A-Za-z0-9_.\-]+)\s*(?:\(\s*)?==", requirement)
            if match:
                found.add(canonical(match.group(1)))
    return found


def test_test_imports_are_covered_by_the_requirements_files():
    declared = pinned_names("requirements.txt") | pinned_names("requirements-dev.txt")
    available = declared | exact_dependencies(declared)
    for module, dists in distributions_for(third_party_imports(source_files(True))).items():
        assert dists & available, f"{module} ({sorted(dists)}) is missing from requirements-dev.txt"


def test_packaging_build_tool_is_installed_by_the_dev_requirements():
    """packaging/build.ps1 aborts without PyInstaller in the venv."""
    assert "pyinstaller" in pinned_names("requirements-dev.txt")
    assert "pyinstaller" not in pinned_names("requirements.txt")


def test_readme_desktop_build_installs_its_tools_first():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Desktop app", 1)[1].split("### MCP server", 1)[0]
    assert "pip install -r requirements-dev.txt" in section
    assert section.index("requirements-dev.txt") < section.index("build.ps1")


def test_readme_documents_a_clean_install():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needed in ("Python 3.12", "python -m venv .venv", ".venv\\Scripts\\Activate.ps1",
                   "pip install -r requirements.txt", "pip install -r requirements-dev.txt",
                   "copy .env.example .env"):
        assert needed in readme, needed
