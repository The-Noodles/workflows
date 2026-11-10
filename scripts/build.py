"""compose.yaml の build: があるサービスをビルドし、registry.noodles.jp に push する。

イメージの名前は registry.noodles.jp/<owner>/<repo>/<サービス>:<コミット>（すべて小文字）。
置き場所がルートでなければ、サービス名の前に置き場所を付ける（Hub の modules/platform/server/compose.ts と同じ決まり）。

ビルドの層は GitHub Actions のキャッシュ（type=gha）に置き、次のビルドで使い回す。変わらなかった層は中身が同じになるので、
レジストリにすでにあるものとして push も飛ばされる。サービスが複数あるときは並べてビルドする。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import IO

import yaml

REGISTRY = "registry.noodles.jp"
# build.yml で入れる railpack と同じ版にする
RAILPACK_FRONTEND = "ghcr.io/railwayapp/railpack-frontend:v0.43.0"


def run(*args: str, log: IO[str] | None, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(args), file=log, flush=True)
    subprocess.run(args, check=True, env=None if env is None else {**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT if log else None)


def image_segment(path: str, service: str) -> str:
    if path in ("", "."):
        return service
    p = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return f"{p}-{service}"


def image_name(repo: str, path: str, service: str, commit: str) -> str:
    return f"{REGISTRY}/{repo.lower()}/{image_segment(path, service)}:{commit}"


def login(config_dir: Path) -> None:
    """GitHub ActionsのOIDCトークンを取り、crane が使う Docker の設定に置く。トークンは数分で切れるので push の直前に呼ぶ。"""
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + REGISTRY
    req = urllib.request.Request(url, headers={"Authorization": "bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(req, timeout=30) as res:
        token = json.load(res)["value"]
    print(f"::add-mask::{token}", flush=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps({"auths": {REGISTRY: {"registrytoken": token}}}))
    path.chmod(0o600)


def build(name: str, context: Path, dockerfile: Path | None, args: dict[str, str], layout: Path, scope: str, log: IO[str] | None) -> None:
    common = ["docker", "buildx", "build", "--provenance=false", "--sbom=false", "--output", f"type=oci,tar=false,dest={layout}"]
    # キャッシュの読み書きには、build.yml が環境変数に出す Actions のトークンが要る
    if os.environ.get("ACTIONS_RUNTIME_TOKEN"):
        common += ["--cache-from", f"type=gha,scope={scope}", "--cache-to", f"type=gha,scope={scope},mode=max,ignore-error=true"]
    for k, v in args.items():
        common += ["--build-arg", f"{k}={v}"]
    if dockerfile is not None:
        run(*common, "-f", str(dockerfile), str(context), log=log)
        return
    plan = layout.parent / f"{name}-railpack-plan.json"
    run("railpack", "prepare", str(context), "--plan-out", str(plan), "--info-out", str(layout.parent / f"{name}-railpack-info.json"), log=log)
    run(*common, "--build-arg", f"BUILDKIT_SYNTAX={RAILPACK_FRONTEND}", "-f", str(plan), str(context), log=log)


def build_and_push(name: str, spec: dict, base: Path, tmp: Path, log: IO[str] | None) -> None:
    app_path = str(os.environ.get("APP_PATH") or ".")
    context = (base / spec.get("context", ".")).resolve()
    df = context / (spec.get("dockerfile") or "Dockerfile")
    raw_args = spec.get("args") or {}
    if isinstance(raw_args, list):
        raw_args = dict(str(a).split("=", 1) for a in raw_args if "=" in str(a))
    args = {str(k): str(v) for k, v in raw_args.items() if v is not None}
    layout = tmp / name
    build(name, context, df if df.exists() else None, args, layout, f"noodles-{image_segment(app_path, name)}", log)
    ref = image_name(os.environ["GITHUB_REPOSITORY"], app_path, name, os.environ["COMMIT"])
    # buildx の builder は ~/.docker にあるので、DOCKER_CONFIG を変えるのは crane だけにする
    docker_config = Path(os.environ["NOODLES_DOCKER_CONFIG"]) / name
    login(docker_config)
    run("crane", "push", str(layout), ref, log=log, env={"DOCKER_CONFIG": str(docker_config)})


def main() -> int:
    base = Path(os.environ.get("APP_PATH") or ".")
    compose = yaml.safe_load((base / "compose.yaml").read_text()) or {}
    targets: dict[str, dict] = {}
    for name, svc in (compose.get("services") or {}).items():
        spec = (svc or {}).get("build")
        if spec is not None:
            targets[name] = {"context": spec} if isinstance(spec, str) else spec
    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        if len(targets) == 1:
            for name, spec in targets.items():
                build_and_push(name, spec, base, Path(tmp), None)
        elif targets:
            # 並べて流すと出力が混ざるので、サービスごとにファイルへ取っておき、終わった順ではなく名前の順にまとめて出す
            logs = {name: open(Path(tmp) / f"{name}.log", "w+") for name in targets}
            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                jobs = {name: pool.submit(build_and_push, name, spec, base, Path(tmp), logs[name]) for name, spec in targets.items()}
            for name, job in jobs.items():
                err = job.exception()
                print(f"::group::{name}" + ("（失敗）" if err else ""), flush=True)
                logs[name].seek(0)
                sys.stdout.write(logs[name].read())
                logs[name].close()
                print("::endgroup::", flush=True)
                if err:
                    print(f"::error::{name} のビルドに失敗しました：{err}", flush=True)
                    failed = True
    if failed:
        return 1
    print(f"{len(targets)}個のイメージを push しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
