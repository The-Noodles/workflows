"""noodles.yaml の形と、コードが読む環境変数の宣言漏れを確かめる。

詳しい確かめは Hub がデプロイのときに行う。ここでは、push の時点で分かる間違いを早めに知らせる。
調べるのは compose.yaml の build: の置き場所の中だけ。別の Dockerfile がある下のディレクトリ（別のイメージ）、
テストと設定のファイルは飛ばす。Dockerfile の ENV と compose.yaml の environment は宣言済みとみなす。
宣言しなくてよい行には、同じ行に noodles-ignore-env と書く。
"""

import os
import re
import sys
from pathlib import Path

import yaml

TOP = {"project", "name", "domain", "branches", "preview", "services", "env", "volumes"}
PROJECT = re.compile(r"^[a-z][a-z0-9-]{0,19}$")
# 実行環境が入れる、宣言しなくてよい変数
BUILTIN = {
    "PORT", "NODE_ENV", "HOSTNAME", "HOST", "PATH", "HOME", "PWD", "TZ", "LANG", "CI", "DEBUG",
    "NODE_OPTIONS", "VERCEL", "npm_package_version", "npm_lifecycle_event",
}
PATTERNS = [
    re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"process\.env\[['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\]"),
    re.compile(r"import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"Deno\.env\.get\(['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\)"),
    re.compile(r"Bun\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"os\.environ(?:\.get)?[\[(]['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    re.compile(r"os\.getenv\(['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    re.compile(r"os\.(?:Getenv|LookupEnv)\(\"([A-Za-z_][A-Za-z0-9_]*)\"\)"),
    re.compile(r"ENV\[['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\]"),
    re.compile(r"env::var\(\"([A-Za-z_][A-Za-z0-9_]*)\"\)"),
]
SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx", ".py", ".go", ".rb", ".rs", ".luau", ".lua"}
TEST_FILE = re.compile(r"(\.(test|spec)\.[a-z]+$)|(^testing\.[a-z]+$)|(\.config\.[a-z]+$)|(_test\.go$)|(^test_.*\.py$)")
SKIP_DIRS = {"test", "tests", "__tests__", "testdata", "node_modules", ".git", "dist", "build", ".next", "vendor", "target", ".venv", "__pycache__", ".noodles-workflows"}


def error(msg: str, file: str | None = None, line: int | None = None) -> None:
    loc = f" file={file}" + (f",line={line}" if line else "") if file else ""
    print(f"::error{loc}::{msg}")


def main(base: Path) -> int:
    ok = True
    spec_path = base / "noodles.yaml"
    compose_path = base / "compose.yaml"
    if not spec_path.exists():
        error(f"{spec_path} がありません")
        return 1
    if not compose_path.exists():
        error(f"{compose_path} がありません")
        return 1
    spec = yaml.safe_load(spec_path.read_text()) or {}
    compose = yaml.safe_load(compose_path.read_text()) or {}
    for k in spec:
        if k not in TOP:
            error(f"noodles.yaml の {k} は使えません", str(spec_path))
            ok = False
    if not PROJECT.match(str(spec.get("project", ""))):
        error("noodles.yaml の project は英小文字・数字・ハイフン（20文字まで）です", str(spec_path))
        ok = False
    services = compose.get("services") or {}
    for name in spec.get("services") or {}:
        if name not in services:
            error(f"noodles.yaml の services.{name} が compose.yaml にありません", str(spec_path))
            ok = False

    declared = set((spec.get("env") or {}).keys()) | BUILTIN
    for svc in services.values():
        env = (svc or {}).get("environment") or {}
        if isinstance(env, list):
            declared |= {str(e).split("=", 1)[0] for e in env}
        else:
            declared |= set(env.keys())

    roots: list[Path] = []
    for svc in services.values():
        spec_build = (svc or {}).get("build")
        if spec_build is None:
            continue
        ctx_dir = spec_build if isinstance(spec_build, str) else spec_build.get("context", ".")
        root = (base / ctx_dir).resolve()
        roots.append(root)
        dockerfile = root / ((spec_build.get("dockerfile") if isinstance(spec_build, dict) else None) or "Dockerfile")
        if dockerfile.exists():
            for line in dockerfile.read_text(errors="ignore").replace("\\\n", " ").splitlines():
                m = re.match(r"\s*ENV\s+(.*)", line)
                if m:
                    declared |= set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", m.group(1))) or {m.group(1).split()[0]}

    missing: dict[str, tuple[str, int]] = {}
    for f in sorted({f for r in set(roots) for f in r.rglob("*")}):
        if f.suffix not in SUFFIXES or not f.is_file() or TEST_FILE.search(f.name):
            continue
        root = next(r for r in roots if f.is_relative_to(r))
        rel = f.relative_to(root)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        if any((root / Path(*rel.parts[:i]) / "Dockerfile").exists() for i in range(1, len(rel.parts))):
            continue
        try:
            lines = f.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if "noodles-ignore-env" in line:
                continue
            for pat in PATTERNS:
                for m in pat.finditer(line):
                    key = m.group(1)
                    if key not in declared and key not in missing:
                        missing[key] = (os.path.relpath(f), i)
    for key, (file, line) in sorted(missing.items()):
        error(f"{key} が noodles.yaml の env にありません（要らなければ行に noodles-ignore-env と書く）", file, line)
    if missing:
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
